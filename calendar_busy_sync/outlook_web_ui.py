"""Outlook's visible browser controls and scoped calendar-grid discovery."""

import re
from datetime import date, datetime, timedelta
from time import monotonic, sleep
from urllib.parse import quote, unquote, urlsplit

from .models import SyncError
from .outlook_web_import import select_mirror, submit_import

_ORIGIN = "https://outlook.cloud.microsoft"
_CALENDAR = _ORIGIN + "/calendar/"
_WEEK = _CALENDAR + "view/week"
_EDITOR_TITLE = 'input[placeholder="Add title"]'
_EDITOR_BODY = '[aria-label="Event body"]'
_ITEM_SEGMENT = "/calendar/item/"
_ITEM_ROUTE = re.compile(r"/calendar/item/[A-Za-z0-9%_+=~-]+")
_ITEM_ID = re.compile(r"[A-Za-z0-9_+=/~:-]+")
_LOAD_ERROR = "Outlook did not display the requested calendar range; no partial read will be used."
_SIGN_IN_ERROR = "Outlook web is signed out or the calendar is unavailable. Run Outlook web sign-in again."
_TIMEOUT = 12

_DOM = r"""
const visible = element => element?.getClientRects().length &&
    getComputedStyle(element).visibility === 'visible';
const elements = (selector, root = document) => [...root.querySelectorAll(selector)].filter(visible);
const unique = items => items.length === 1 ? items[0] : null;
const labels = element => [element.getAttribute('aria-label'), element.title, element.innerText?.trim()];
const controls = (text, root = document) =>
    elements('button, [role=button], [role=menuitem], [role=option], a[href]', root)
      .filter(element => !element.disabled && element.getAttribute('aria-disabled') !== 'true' &&
                         labels(element).includes(text));
const leaf = text => unique(elements('body *').filter(element =>
    !element.children.length && element.textContent.trim() === text));
const press = element => {
    if (!element) return false;
    element.click();
    return true;
};
const calendarToggle = /^((?:De)?[Ss]elect) calendar '(.*)'$/;
const toggles = () => elements('button[title], [role=button][title]')
    .map(element => [element, element.title.match(calendarToggle)])
    .filter(([, match]) => match);
const isScoped = name => {
    const calendars = toggles();
    const selected = calendars.filter(([, match]) => /deselect/i.test(match[1]));
    return selected.length === 1 && selected[0][1][2] === name &&
        calendars.filter(([, match]) => match[2] === name).length === 1;
};
const gridItems = () => elements('[data-calitemid]').flatMap(element => {
    const label = element.matches('[role=button][aria-label]') ? element :
        unique(elements('[role=button][aria-label]', element));
    return label ? [[element.getAttribute('data-calitemid'), label]] : [];
});
"""

_WEEK_LABEL = re.compile(
    r"^Go to (?:previous|next) week\s+(\d{4}), ([A-Za-z]+) (\d{1,2})\s*[–-]\s*"
    r"(?:(\d{4}),\s*)?(?:([A-Za-z]+) )?(\d{1,2})$"
)


class OutlookUI:
    def __init__(self, browser):
        self.browser = browser

    def execute(self, script, *args):
        return self.browser.execute(_DOM + script, *args)

    def wait(self, condition, *args, timeout=_TIMEOUT, message=_LOAD_ERROR, headed_sign_in=False):
        deadline = monotonic() + timeout
        while True:
            # OWA's redirects can appear during manual sign-in, but never during unattended sync.
            if not headed_sign_in and not self.on_outlook():
                raise SyncError(_SIGN_IN_ERROR)
            result = self.execute(condition, *args)
            if result or isinstance(result, list):
                return result
            if monotonic() >= deadline:
                if message is None:
                    return None
                raise SyncError(message)
            sleep(0.2)

    def on_outlook(self):
        return self.browser.execute("return location.origin === arguments[0];", _ORIGIN)

    def open_calendar(self):
        self.browser.navigate(_WEEK)
        self.wait("return controls('New event').length === 1;", message=_SIGN_IN_ERROR)
        self.show_calendars()
        self.click_control("Week")
        self.wait("return location.pathname.replace(/\\/$/, '') === '/calendar/view/week';")

    def await_sign_in(self):
        self.wait(
            "return location.origin === arguments[0] && controls('New event').length === 1;",
            _ORIGIN,
            timeout=600,
            message=_SIGN_IN_ERROR,
            headed_sign_in=True,
        )

    def show_calendars(self):
        self.execute("return press(unique(controls('Show navigation pane')));")
        self.wait(
            """
            const groups = elements('li[aria-label] button[aria-expanded="false"]')
                .filter(button => labels(button).includes(button.closest('li[aria-label]').getAttribute('aria-label')));
            if (groups.length) {
                press(groups[0]);
                return false;
            }
            if (press(leaf('Show all'))) return false;
            return toggles().length > 0;
            """
        )

    def select_calendar(self, name):
        self.show_calendars()
        stable_since = monotonic()
        deadline = stable_since + _TIMEOUT
        while monotonic() < deadline:
            if not self.on_outlook():
                raise SyncError(_SIGN_IN_ERROR)
            state = self.execute(
                """
                const calendars = toggles();
                const matches = calendars.filter(([, match]) => match[2] === arguments[0]);
                if (matches.length !== 1) return null;
                if (!/deselect/i.test(matches[0][1][1])) return matches[0][0].title;
                const others = calendars.filter(([, match]) =>
                    match[2] !== arguments[0] && /deselect/i.test(match[1]));
                return others.length ? others[0][0].title : '';
                """,
                name,
            )
            if state:
                self.click_control(state)
                self.wait("return !toggles().some(([element]) => element.title === arguments[0]);", state)
                stable_since = monotonic()
            elif state is None:
                stable_since = monotonic()
            elif monotonic() - stable_since >= 2:
                return
            sleep(0.2)
        raise SyncError("Outlook did not settle on exactly one calendar with the configured name.")

    def assert_owned_calendar(self, name):
        if not self.execute(
            """
            const matches = toggles().filter(([, match]) => match[2] === arguments[0]);
            return matches.length === 1 &&
                matches[0][0].closest('li[aria-label]')?.getAttribute('aria-label') === 'My calendars';
            """,
            name,
        ):
            raise SyncError("The Outlook mirror must be an owned calendar in My calendars.")

    def assert_scope(self, name):
        if not self.execute("return isScoped(arguments[0]);", name):
            raise SyncError("Outlook is not showing only the requested calendar; sync stopped.")

    def week_start(self):
        labels = self.wait(
            """
            const previous = unique(elements('button[aria-label^="Go to previous week"]'));
            const next = unique(elements('button[aria-label^="Go to next week"]'));
            if (!previous || !next) return null;
            return [previous.getAttribute('aria-label'), next.getAttribute('aria-label')];
            """
        )
        previous_start, previous_end = _parse_week_label(labels[0])
        next_start, next_end = _parse_week_label(labels[1])
        if (
            previous_end - previous_start != timedelta(days=6)
            or next_end - next_start != timedelta(days=6)
            or next_start - previous_start != timedelta(days=14)
        ):
            raise SyncError("Outlook did not display a full seven-day Week view; use Week, not Work week.")
        return previous_start + timedelta(days=7)

    def go_to_week(self, day):
        # Use the date ranges from Outlook's navigation controls, not the URL as a completion signal.
        week = self.week_start()
        while not week <= day < week + timedelta(days=7):
            offset = 7 if day >= week + timedelta(days=7) else -7
            expected = week + timedelta(days=offset)
            direction = "next" if offset > 0 else "previous"
            label = f"Go to {direction} week"
            previous = self.execute(
                """
                const node = unique(elements(`button[aria-label^="${arguments[0]}"]`));
                return node ? node.getAttribute('aria-label') : null;
                """,
                label,
            )
            self.wait("""return press(unique(elements(`button[aria-label^="${arguments[0]}"]`)));""", label)
            self.wait(
                """
                const button = unique(elements(`button[aria-label^="${arguments[0]}"]`));
                return button && button.getAttribute('aria-label') !== arguments[1];
                """,
                label,
                previous,
            )
            week = self.week_start()
            if week != expected:
                raise SyncError(_LOAD_ERROR)
        return week

    def sample_week(self, name):
        # A stable set of visible cards is discovery evidence, never evidence of absence.
        snapshot = None
        deadline = monotonic() + _TIMEOUT
        while monotonic() < deadline:
            items = self.wait(
                """
                if (!isScoped(arguments[0]) || elements('[aria-busy="true"], [role=progressbar]').length) {
                    return null;
                }
                const items = gridItems();
                if (items.length !== elements('[data-calitemid]').length) return null;
                return items.map(([id, card]) => [id, card.getAttribute('aria-label')]);
                """,
                name,
            )
            if items == snapshot:
                return items
            snapshot = items
            sleep(0.5)
        raise SyncError(_LOAD_ERROR)

    def click_control(self, label, *, message=_LOAD_ERROR):
        self.wait("return press(unique(controls(arguments[0])));", label, message=message)

    def click_import_step(self, label):
        self.wait(
            """
            const choices = controls(arguments[0]);
            return press(choices.length ? unique(choices) : leaf(arguments[0]));
            """,
            label,
            message="Outlook did not present the expected calendar import control.",
        )

    def open_grid_editor(self, name, item_id):
        self.assert_scope(name)
        card_opened = self.wait(
            """
            if (!isScoped(arguments[0])) return false;
            const matches = gridItems().filter(([id]) => id === arguments[1]);
            return press(unique(matches.map(([, card]) => card)));
            """,
            name,
            item_id,
            timeout=3,
            message=None,
        )
        if not card_opened:
            return None
        if not self.wait("return press(unique(controls('Edit')));", timeout=4, message=None):
            return None
        return self.editor_url()

    def editor_url(self):
        url = self.wait(
            """
            if (!location.pathname.startsWith('/calendar/item/')) return null;
            return elements(arguments[0]).length === 1 && elements(arguments[1]).length === 1 ? location.href : null;
            """,
            _EDITOR_TITLE,
            _EDITOR_BODY,
            timeout=5,
            message=None,
        )
        if url and valid_editor_url(url):
            return url
        return None

    def reopen_editor(self, url):
        if not valid_editor_url(url):
            return False
        self.browser.navigate(url)
        return self.editor_url() == url

    def open_item(self, item_id):
        if not _ITEM_ID.fullmatch(item_id):
            return False
        self.browser.navigate(_ORIGIN + _ITEM_SEGMENT + quote(item_id, safe=""))
        url = self.editor_url()
        return bool(url and item_id_from_url(url) == item_id)

    def calendar_binding(self, name):
        return self.execute(
            """
            const prefix = arguments[0] + ' (';
            const control = unique(elements('button[aria-label]').filter(element => {
                const label = element.getAttribute('aria-label');
                return label.startsWith(prefix) && label.endsWith(')') && label.length > prefix.length + 1;
            }));
            return control?.getAttribute('aria-label');
            """,
            name,
        )

    def editor_content(self):
        return self.execute(
            """
            const title = unique(elements(arguments[0]));
            const body = unique(elements(arguments[1]));
            return title && body ? [title.value, body.innerText] : null;
            """,
            _EDITOR_TITLE,
            _EDITOR_BODY,
        )

    def close_editor(self):
        if not self.wait("return press(unique(controls('Close')));", timeout=3, message=None):
            raise SyncError("Outlook did not close the event editor; sync stopped.")
        self.wait("return elements(arguments[0]).length === 0;", _EDITOR_TITLE)

    def show_editor_timing(self, *, message="Outlook did not display the appointment date and time fields."):
        return self.wait(
            r"""
            if (elements('input[aria-label="Start date"]').length === 1) return true;
            let root = unique(elements(arguments[0]));
            while (root && !controls('Scheduler', root).length) root = root.parentElement;
            if (!root) return false;
            const summary = unique(elements('div[tabindex]', root).filter(element =>
                /\b\d{4}-\d{2}-\d{2}\b.*\d{1,2}:\d{2}\s*[AP]M/i.test(element.innerText)
                && !element.querySelector('div[tabindex]')));
            press(summary);
            return false;
            """,
            _EDITOR_TITLE,
            message=message,
        )

    def editor_times(self, *, message="Outlook did not display unambiguous timed appointment fields."):
        return self.wait(
            """
            const field = name => unique(elements(`input[aria-label="${name}"]`));
            const startDate = field('Start date');
            const startTime = field('Start time');
            const endTime = field('End time');
            if (!startDate || !startTime || !endTime) return null;
            const allDay = unique(elements('[aria-label="All day"]'));
            if (allDay?.checked || allDay?.getAttribute('aria-checked') === 'true') return null;
            return [startDate.value, startTime.value, field('End date')?.value ?? null, endTime.value];
            """,
            message=message,
        )

    def editor_availability(self):
        return self.wait(
            r"""
            const states = ['Free', 'Working elsewhere', 'Tentative', 'Busy', 'Out of office'];
            const matches = elements('button').flatMap(button =>
                labels(button).flatMap(label => (label || '').split('\n').map(line => line.trim()))
                    .filter(line => states.includes(line)));
            return unique([...new Set(matches)]);
            """,
            timeout=4,
            message=None,
        )

    def _click_editor_action(self, action, url, mailbox, title, note):
        self.wait(
            """
            if (location.href !== arguments[1]) return false;
            const title = unique(elements('input[placeholder="Add title"]'));
            const body = unique(elements('[aria-label="Event body"]'));
            const mailbox = unique(elements('button[aria-label]').filter(button =>
                button.getAttribute('aria-label') === arguments[2]));
            if (!mailbox || title?.value !== arguments[3] || body?.innerText !== arguments[4]) return false;
            return press(unique(controls(arguments[0])));
            """,
            action,
            url,
            mailbox,
            title,
            note,
            message="Outlook did not confirm the exact managed editor and action; unchanged.",
        )

    def import_calendar_file(self, path, name, *, before_submit=None):
        self.click_import_step("Add calendar")
        self.click_import_step("Upload from file")
        upload = self.wait(
            "return unique([...document.querySelectorAll('input[type=file]')]);",
            message="Outlook did not present exactly one calendar upload control.",
        )
        self.browser.type_text(upload, str(path))
        self.wait(
            r"""
            let form = document.querySelector('input[type=file]');
            while (form && !form.innerText?.includes('Upload from file')) form = form.parentElement;
            const destination = form && unique(elements('div[role=button],div[role=combobox]', form));
            if (!destination || !destination.innerText.split('\n').includes('Calendar')) return false;
            if (destination.innerText.split('\n').includes(arguments[0])) return false;
            return press(destination);
            """,
            name,
            message="Outlook did not identify a default primary calendar distinct from the dedicated mirror.",
        )
        self.wait("return elements('[role=listbox] [role=option]').length > 0;")
        select_mirror(self.execute, name)
        self.wait(
            r"""
            let form = document.querySelector('input[type=file]');
            while (form && !form.innerText?.includes('Upload from file')) form = form.parentElement;
            const destination = form && unique(elements('div[role=button],div[role=combobox]', form));
            return destination?.innerText.split('\n').includes(arguments[0]);
            """,
            name,
            message="Outlook did not confirm the dedicated upload destination.",
        )
        if before_submit is not None:
            before_submit()
        submit_import(self.execute, name)
        confirmation = f'Imported "{path.name}" to {name}.'
        self.wait(
            "return document.body.innerText.split('\\n').some(line => line.trim() === arguments[0]);",
            confirmation,
            message="Outlook did not confirm the import. No second import was attempted.",
        )

    def delete_editor_event(self, url, mailbox, title, note):
        self._click_editor_action("Delete", url, mailbox, title, note)
        self.wait(
            """
            if (location.href !== arguments[0]) return false;
            const dialog = unique(elements('[role=dialog],[role=alertdialog]').filter(element =>
                element.innerText.startsWith('Delete event') &&
                element.innerText.includes('Are you sure you want to delete this event?')));
            return dialog ? press(unique(controls('Delete', dialog))) : false;
            """,
            url,
            message="Outlook did not present the expected single-event delete confirmation.",
        )
        self.wait(
            "return document.body.innerText.split('\\n').some(line => /^Event deleted.*to Undo\\./.test(line));",
            message="Outlook did not confirm deletion. No second delete was attempted.",
        )
        # Outlook delays the remote mutation while Undo is offered; navigation can cancel it.
        self.wait(
            "return !document.body.innerText.split('\\n').some(line => /^Event deleted.*to Undo\\./.test(line));",
            timeout=45,
            message="Outlook did not finish its deletion undo window; no second delete was attempted.",
        )


def valid_editor_url(url: str) -> bool:
    parts = urlsplit(url)
    return (
        url.startswith(_ORIGIN + "/")
        and _ITEM_ROUTE.fullmatch(parts.path) is not None
        and not parts.query
        and not parts.fragment
    )


def item_id_from_url(url: str) -> str:
    return unquote(urlsplit(url).path.removeprefix(_ITEM_SEGMENT))


def _parse_week_label(label: str) -> tuple[date, date]:
    match = _WEEK_LABEL.fullmatch(label)
    if not match:
        raise SyncError(_LOAD_ERROR)
    year, month, day, final_year, final_month, final_day = match.groups()
    try:
        start = datetime.strptime(f"{year} {month} {day}", "%Y %B %d").date()
        end = datetime.strptime(f"{final_year or year} {final_month or month} {final_day}", "%Y %B %d").date()
        if end < start and final_year is None:
            end = end.replace(year=end.year + 1)
    except ValueError:
        raise SyncError(_LOAD_ERROR) from None
    return start, end
