# FAQ and Troubleshooting — AI-Assisted Bank Reconciliation

## 1. Frequently Asked Questions

### About the system

| Question | Answer |
|----------------------------------------------|----------------------------------------------------------------------------|
| Does the system approve anything by itself? | No. Every record needs a decision by a person before it can count as reconciled. The system only suggests, and the suggestion's confidence decides only the order items are shown in |
| Where does the AI come in? | In three places, none of which decides: scoring possible matches, ranking them, and a calibrated "how sure" figure. Fixed rules settle exact matches first; risk is set by fixed rules too, never by the model. The optional AI summary only restates the evidence |
| Can I trust a "fairly sure" suggestion? | Read the evidence. "What doesn't fit" lists anything that argues against the suggestion, and it is never left out. A suggestion marked "unsure" can be right, and one marked "fairly sure" can be wrong |
| Does it learn from my decisions? | No. Decisions are recorded, but the model is never retrained from individual reviewer decisions. Its calibration was fitted once, on a separate month |
| Does it post journal entries? | No. It proposes correcting entries and records who approved them. The accounting team posts them in the books |
| What file formats can it import? | CSV only: the bank statement, the books for the cash account, last month's open items, and optional control totals. Bank formats such as MT940, BAI2 or OFX are not supported |
| How many accounts and months? | One bank account, one month at a time |
| Is any data sent to the internet? | Only if AI summaries are switched on, and only for data declared synthetic. With summaries off (the default), nothing leaves the computer |

### About reviewing

| Question | Answer |
|----------------------------------------------|----------------------------------------------------------------------------|
| Why can't I approve this item? | The reason is written next to the greyed-out button. Most often: the item is high risk and needs a Senior Accountant or the Controller; you reviewed or escalated it yourself; or the month is closed |
| Why must I write a comment? | A comment is needed for every choice except approving a low or medium risk item. It tells the next person, and an auditor, why |
| Why can't a possible duplicate be approved? | Approving would mark money nobody has explained as reconciled. Investigate it, then pair it by hand, leave it open, or ask a senior reviewer |
| I approved something by mistake. Can I undo it? | Yes, while the month is open. Ask a Senior Accountant or the Controller (not yourself) to open the item and choose **Reverse this decision** with a reason. Your decision is never edited or deleted: the reversal is recorded beside it and the item goes back for review. After the month is closed, it must be reopened first |
| What does "Approve exact matches in one go" record? | One decision per item, under your name, exactly as if you had approved each one. Only exact matches with low risk can be included, and you can untick any |
| What are "Original records, exactly as received"? | Each record as it arrived in its file, before descriptions and references were tidied for comparison. Use them to check against the bank statement or the source document |
| Why is an item "approved" but not "reconciled"? | An item counts as reconciled only after the reports confirm it. Choose **Update the reports** |
| What is "Show details"? | It reveals the rule references, scores and record codes behind each screen. It is for auditors and administrators; everyday work does not need it |

### About closing

| Question | Answer |
|----------------------------------------------|----------------------------------------------------------------------------|
| Why is "Sign off and close the month" greyed out? | Either you are not the Controller, a step in **Before you can close** is not ticked yet, or you were the only reviewer this month (someone else must sign off) |
| The difference is not zero. Can I still close? | Yes. Explain the difference in your comment; it is recorded with the sign-off and printed on the Reconciliation Summary |
| What does closing do? | It records the statement and a fingerprint of the audit trail, then locks the month: no decision, adjustment or import can change it |
| How do I change something after closing? | A Staff or Senior Accountant asks to reopen it, with a reason. The Controller, not the person who asked, allows or refuses it. A reopened month must be signed off again |
| What does "No tampering detected" mean? | Every audit entry still matches its fingerprint, and each fingerprint includes the one before it, so changing any past entry would show. It proves the trail was not edited; it cannot stop someone who holds the database file from deleting it, which is why the signed report is kept outside the database |

## 2. Troubleshooting

### Installing and starting

| Symptom | Cause | Fix |
|----------------------------------------|----------------------------------|----------------------------------------------------------|
| `ModuleNotFoundError: No module named 'jinja2'` (or `fastapi`, `sklearn`) | Packages not installed for the Python being used | `python3 -m pip install -r requirements.txt`, using the same `python3` that runs the app. If you made a `.venv`, activate it first |
| `ModuleNotFoundError: No module named 'tomllib'` | Python older than 3.11 | Install Python 3.11 or later and reinstall the packages |
| `python3: command not found` (Windows) | Windows names it `python` | Use `python` in place of `python3` |
| `Run 'python -m app.cli init' first.` | No database yet | `python3 -m app.cli init` |
| `... is already initialized.` | `init` was run before | Nothing to do: the database is ready |
| `config/default.toml not found; run from the repository root` | Terminal is in another folder | `cd` into the `bank_reconciliation` folder |
| `[Errno 48] Address already in use` (or `10048` on Windows) | The interface is already running, or another program uses port 8000 | Stop the other one with Ctrl+C, or `python3 -m app.cli serve --port 8001` and open <http://127.0.0.1:8001> |
| The page does not open | The interface is not running | Start it with `python3 -m app.cli serve` and keep that terminal open |
| `zsh: bad pattern` or `command not found` after pasting a command | The terminal pasted hidden characters, or a comment, along with the command | Type the command, or paste one command at a time without trailing comments |

### Using the interface

| Symptom | Cause | Fix |
|----------------------------------------|----------------------------------|----------------------------------------------------------|
| Every button is greyed out | No person chosen | Choose a name from **Acting as** at the top right |
| The person I chose is forgotten after restarting | Restarting clears the browser session | Choose again, or set `BANKREC_SESSION_KEY` before starting (GDE-01 section 8) |
| The page looks unstyled or out of date | Browser kept an old copy | Reload the page (Cmd+Shift+R on macOS, Ctrl+F5 on Windows) |
| "A file failed its checks, so nothing was matched" | A file-level check failed, such as control totals that do not agree | Open **Problems that stop matching** on the files page, correct the file, and start the month again. The failed attempt stays on record |
| `... has already been imported into run ...` | The same file was uploaded twice | Each file can be imported once per month. Start a new month to load a corrected file |
| "This period is closed. It must be reopened before anything can change." | The month is locked | Ask to reopen it (section 1, About closing) |
| "The reports are up to date" stays crossed | A decision was made after the last update | Choose **Update the reports** again |
| An item is missing from a list | It was decided, or another decision settled it | Open **Audit trail** and filter by **Decision recorded**; nothing is ever deleted |
| `database is locked` | Two copies of the interface or a command are using the same database | Stop one of them; use one interface process at a time |

### AI summaries

| Symptom | Cause | Fix |
|----------------------------------------|----------------------------------|----------------------------------------------------------|
| No "Write a plain-words summary" button | Summaries are off (the default) | Turn them on as described in GDE-01 section 7 |
| "The AI service didn't answer" | The provider is busy (HTTP 503), the key is wrong, or there is no network | Run `python3 -m app.infra.genai.gemini`. For `503`, choose another `flash` or `flash-lite` model in `config/local.toml`; for `401` or `403`, check the key in `.env` |
| `CERTIFICATE_VERIFY_FAILED` | This Python has no certificate bundle (common with some macOS installs) | `python3 -m pip install certifi`, or on a python.org install run **Install Certificates.command** in `/Applications/Python 3.x/` |
| `404: model ... not found` | The model has been retired | Run `python3 -m app.infra.genai.gemini` and pick a model from the list it prints |
| "The AI's wording was discarded" | The AI went beyond restating the evidence (for example, recommended a decision) | Nothing to fix: the system saved a plain summary of the evidence instead. This is the guard working |
| Summary says "written by the system, not by AI" | The AI did not answer or was discarded, so the system's own summary is shown | Try again later; the evidence on the page is unaffected |

### Audit trail and tests

| Symptom | Cause | Fix |
|----------------------------------------|----------------------------------|----------------------------------------------------------|
| "Tampering detected at entry N" | A stored audit entry no longer matches its fingerprint | Stop work and keep the database file unchanged. Compare with the last backup and the chain fingerprint printed on the signed Reconciliation Summary |
| Tests fail after editing code | A rule in the Developer Guide (GDE-03 section 4) was broken | Read the failing test's name and docstring; each test states the rule it checks |
| Tests print a warning about `httpx` | A notice from a test library | Already filtered in `pytest.ini`; if it appears, it does not affect results |

## 3. Getting More Help

| Need | Where |
|-----------------------------------|----------------------------------------------------|
| Install, configure, back up | Installation and Deployment Guide (GDE-01) |
| How to reconcile a month | User Guide (GDE-02), and **Help** inside the interface |
| How the code works | Developer Guide (GDE-03) and System Design (DOC-05) |
| Why a result is what it is | Evaluation Report (DOC-07) |
