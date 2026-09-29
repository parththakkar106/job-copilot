# job copilot

job copilot helps you apply to jobs faster. It finds open early-career roles, fills in
most of each application form for you, and leaves the final Submit click to you.

## What it does

1. **Finds jobs.** It checks company job boards on Greenhouse, Lever, Ashby, and Workday
   every day. It keeps roles that match the career paths and experience levels you pick, are in
   the US, are recent, and that you have not applied to yet.
2. **Fills applications.** A Chrome extension opens each job and fills in your name,
   contact details, work authorization, resume, and any answers you saved before.
3. **Asks you when it is unsure.** If a question has no saved answer, the extension leaves
   it for you instead of guessing.
4. **Never submits for you.** You review each form and click Submit yourself.

It is built for 5 to 8 careful applications a day, not for mass applying.

## Quick setup

You need Python 3.10 or later and Google Chrome.

1. Run the setup script. It installs dependencies into a `.venv` folder, creates your
   config files, builds the extension, and opens the console in your browser:

   ```bash
   python bootstrap.py
   ```

2. In the console, fill in **Your details**: name, contact, work authorization, and EEO
   answers.
3. Load the extension. Open `chrome://extensions`, turn on **Developer mode**, click
   **Load unpacked**, and select the `extension/` folder. The Setup page has buttons that
   copy the address and the folder path.
4. On the **Search** page, pick your career paths. There are 20, such as software,
   cybersecurity, QA and testing, data science, and DevOps. Each comes with its keywords
   filled in. Then pick the levels: entry (0 to 3 years), mid (4 to 10), and senior (10 or
   more).
5. On the **Setup** page, upload one resume PDF for each path you picked.
6. Optional: edit the companies on the **Companies** page. Ten examples are already there.

After setup, start everything with:

```bash
python start.py
```

The console opens at http://127.0.0.1:8000.

## Optional extras

The tool works without any of these.

| Extra | What it adds |
|---|---|
| **Telegram bot** | Answer open questions from your phone |
| **Email verification codes** | Reads and fills the code some job boards email you at submit |
| **Workday password** | Signs in to, or creates, your account on each Workday job board |
| **AI fill pass** | Drafts answers for free-text questions and checks the filled form |
| **Daily scheduled run** | Finds new jobs and prepares applications every morning |

The [full guide](docs/guide.md) explains how to set up each one.

## Your data stays private

Your details, answers, and resumes stay on your computer. Your config files are ignored by
git, so you cannot commit them by accident. The console has no login, so keep it on your
own machine.

## Learn more

- [Full guide](docs/guide.md): every command, config file, and option.
- [Bridge setup](bridge/README.md): Telegram, email codes, and the AI pass.
- [Daily scheduled run](docs/autonomous_run.md): the unattended morning run.
