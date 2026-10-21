# Installation and Deployment Guide — AI-Assisted Bank Reconciliation

## 1. What You Will Have

A reconciliation workstation that runs on one computer: a local database, the reviewer interface in your web browser at <http://127.0.0.1:8000>, a command line for setup and checks, and the August 2026 sample month ready to reconcile. Nothing is sent over the internet unless you switch on the optional AI summaries (section 7).

## 2. Requirements

| Item | Requirement |
|------------------|--------------------------------------------------------------|
| Operating system | macOS 12 or later, Windows 10 or 11, or a current Linux distribution |
| Python | 3.11 or later. Check with `python3 --version` (Windows: `python --version`) |
| Disk | About 300 MB for Python packages, plus about 50 MB per reconciled month |
| Browser | Any current Chrome, Edge, Firefox or Safari |
| Network | Needed once, to download the packages. Not needed to run |
| Git | Optional. You can download the project as a ZIP instead |

On Windows, replace `python3` with `python` in every command in this guide.

## 3. Install

Run each command on its own, from a terminal.

**1. Get the code.** Either clone it:

```bash
git clone https://github.com/uday269/bank_reconciliation.git
cd bank_reconciliation
```

or download the ZIP from the GitHub page (**Code → Download ZIP**), unzip it, and open a terminal in the unzipped `bank_reconciliation` folder. Every command below runs from that folder.

**2. (Recommended) Create a private Python environment**, so the packages do not mix with anything else on the computer:

| System | Create | Activate (each new terminal) |
|--------------|---------------------------|----------------------------------|
| macOS, Linux | `python3 -m venv .venv` | `source .venv/bin/activate` |
| Windows | `python -m venv .venv` | `.venv\Scripts\activate` |

Skip this step if you use Anaconda or already manage environments another way.

**3. Install the packages:**

```bash
python3 -m pip install -r requirements.txt
```

This installs scikit-learn, FastAPI, Uvicorn, Jinja2, pytest and their dependencies. Nothing else is downloaded later.

**4. Create the database:**

```bash
python3 -m app.cli init
```

This creates `reconciliation.db` with the chart of accounts, the bank account (Lone Peak Commercial Bank, Operating Checking ending 7310) and four people:

| ID | Person | Role |
|----|------------------|-----------------------|
| 1 | Maya Castillo | Staff Accountant |
| 2 | Ethan Brooks | Staff Accountant |
| 3 | Priya Raman | Senior Accountant |
| 4 | Daniel Okafor | Controller |

Running `init` again is safe: it reports that the database already exists and changes nothing.

## 4. Check the Installation

| Check | Command | Expected result |
|--------------------|--------------------------------|------------------------------------------------|
| Tests | `python3 -m pytest -q` | `244 passed` (about two minutes) |
| Database | `python3 -m app.cli status` | `No runs yet.` on a new install |
| Interface | `python3 -m app.cli serve` | `Reviewer interface: http://127.0.0.1:8000` |

Open <http://127.0.0.1:8000>, choose a person from **Acting as** at the top right, and the Home page appears. Press **Ctrl+C** in the terminal to stop the interface.

## 5. Load the Sample Month

There are two ways to start August 2026. Both give identical results, because the sample data is generated from a fixed seed.

| From the interface | From the command line |
|-------------------------------------------------------|--------------------------------------|
| Act as Maya Castillo → **New month** → **Start the month** (1 Aug to 31 Aug 2026) → upload the four files from `data/august_2026/` → **Check and match** | `python3 -m app.cli run` |

The four files are `bank_statement.csv`, `gl_cash_detail.csv`, `carry_in_items.csv` and `run_control.csv`. After matching, Home shows 1,257 records across five review lists. The User Guide (GDE-02) takes you through the review and the close.

## 6. Configuration

Settings live in `config/default.toml`. Do not edit it; put your changes in **`config/local.toml`**, which overrides it and is never committed. Only include the settings you change.

| Setting | Section | Default | Meaning |
|-----------------------------------|-----------------|---------------------|---------------------------------|
| `senior_approval_amount_cents` | `[parameters]` | `1000000` | Items at or above $10,000.00 need a Senior Accountant or the Controller |
| `timing_window_business_days` | `[parameters]` | `3` | How far apart bank and book dates may be and still match |
| `outstanding_check_stale_days` | `[parameters]` | `45` | Uncleared checks older than this are flagged |
| `database` | `[paths]` | `reconciliation.db` | Where the database file lives |
| `report_output` | `[paths]` | `reports` | Where report packages are written |
| `enabled`, `provider`, `model` | `[genai]` | off | Optional AI summaries (section 7) |

Each month keeps a copy of the settings in force when it was started, so changing a setting later never changes the results of a month already started or closed. All thresholds are listed in the Requirements Specification (PRM-01..10).

## 7. Optional: AI Summaries

The item page can show a short plain-English summary of the evidence, written by Google Gemini. It is off by default. It only restates evidence already on the page: it never scores, ranks, decides or approves anything, and the system works fully without it.

1. Get a free API key at <https://aistudio.google.com> (**Get API key**).
2. Copy `.env.example` to `.env` in the project folder and paste the key after `GEMINI_API_KEY=`. Git ignores `.env`; never commit it.
3. Add to `config/local.toml`:

```toml
[genai]
enabled = true
provider = "gemini"
model = "gemini-3.5-flash-lite"
```

4. Test it:

```bash
python3 -m app.infra.genai.gemini
```

The command lists the models your key can use and sends one test request. Look for `generated: True`. If it reports `HTTP 503` the model is busy at Google: try another `flash` or `flash-lite` model from the list.

The sample data is synthetic. The system refuses to send anything to an external service unless `data_classification` is `"synthetic"`; do not change that setting for real bank data.

## 8. Deployment

The system is built for **one workstation or a small trusted team on one computer**. Read the limits below before running it anywhere else.

| Topic | Guidance |
|------------------|--------------------------------------------------------------------------------|
| Who can reach it | By default only the computer it runs on (`127.0.0.1`). Keep it that way |
| No sign-in | People are chosen from **Acting as**, not authenticated. Roles and separation of duties are enforced against the person chosen, but anyone who can open the page can choose any person. Never expose the interface to a network you do not fully trust, and never to the internet |
| Shared computer | To let others on a trusted office network reach it: `python3 -m app.cli serve --host 0.0.0.0 --port 8000`. Only do this behind the organization's firewall, and only after section 9 |
| Staying signed in | Restarting the interface clears who people are acting as. To keep the choice across restarts, set a fixed secret first: `export BANKREC_SESSION_KEY=` followed by a long random value (Windows: `set BANKREC_SESSION_KEY=...`) |
| Real bank data | The system has been built and evaluated on synthetic data only. Before using real data, add real authentication, run it on a managed server, and keep AI summaries off |
| Database | One SQLite file. One interface process at a time; do not open it from two computers over a shared drive |

## 9. Backups and Records

| What | Where | How |
|---------------------------|----------------------------------|----------------------------------------------------------|
| Database | `reconciliation.db` | Stop the interface, then copy the file. Copy `reconciliation.db-wal` too if it exists |
| Signed report packages | `reports/run_<number>/` | Copy the whole folder to the archive after each sign-off |
| Settings | `config/local.toml`, `.env` | Keep `.env` out of shared folders; it holds the API key |

The audit trail lives inside the database. Each sign-off records a fingerprint of the trail (the chain head), and the exported Reconciliation Summary prints it. Keeping that report outside the database is what lets anyone later prove the trail was not rebuilt.

## 10. Updating, Resetting and Removing

| Task | Steps |
|--------------------|--------------------------------------------------------------------------------|
| Update the code | Back up (section 9), then `git pull` and `python3 -m pip install -r requirements.txt`. Run the tests |
| Start again | `python3 -m app.cli reset --force`, then `python3 -m app.cli init`. This deletes every month, decision and audit record in the database; back up first |
| Remove | Delete the `bank_reconciliation` folder. If you made a `.venv`, it is inside that folder and goes with it |

Problems during installation are covered in the FAQ and Troubleshooting guide (GDE-04).
