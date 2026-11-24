"""Select the mirror on Outlook's visible calendar import form."""

from .models import SyncError

_SELECT_MIRROR = """
const options = [...document.querySelectorAll('[role=listbox] [role=option]')];
const matches = options.filter(option => option.getClientRects().length &&
    [...option.querySelectorAll('span')].some(span => span.textContent.trim() === arguments[0]));
if (matches.length !== 1) return false;
matches[0].click();
return true;
"""

_SUBMIT_IMPORT = """
let form = document.querySelector('input[type=file]');
while (form && !form.innerText?.includes('Upload from file')) form = form.parentElement;
if (!form) return false;
const destination = form.querySelector('div[role=button],div[role=combobox]');
const submit = [...form.querySelectorAll('button')].find(b => b.innerText === 'Add to calendar');
if (!destination?.innerText.split('\\n').includes(arguments[0]) || !submit || submit.disabled) return false;
submit.click();
return true;
"""


def select_mirror(execute_script, name):
    """Only an option in the open picker counts, never a matching sidebar label."""
    if not execute_script(_SELECT_MIRROR, name):
        raise SyncError("The import calendar picker did not identify exactly one mirror.")


def submit_import(execute_script, name):
    """Recheck the displayed destination immediately before submitting the file."""
    if not execute_script(_SUBMIT_IMPORT, name):
        raise SyncError("The import form did not confirm the mirror; no import was submitted.")
