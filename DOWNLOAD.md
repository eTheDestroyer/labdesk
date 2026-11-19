# Downloading LabDesk with git

This guide shows how to download LabDesk with `git`, install it and open it, on **Linux** and on **Windows**.
Do the steps in order and copy one block at a time.

* [Linux](#linux)
* [Windows](#windows)
* [Getting a newer version later](#getting-a-newer-version-later)

The repository is **private**. You need a GitHub account that was given access to it, and you sign in once per PC
(step 2 on each system).

> **Before you start, on any system:** LabDesk is a front end for **Cisco CML**. It does not include CML or router
> images. You need a working CML-Free VM that this PC can reach, and its login (usually `admin` and the password you
> chose during CML setup). Check: open `https://<CML address>` in a browser and log in.

---

## Linux

Tested on Debian 13 with KDE. Commands go in a terminal (Konsole).

### 1. Install git and the other packages

```bash
sudo apt update
sudo apt install git gh python3 python3-venv konsole openssh-client libvirt-clients libxcb-cursor0
```

Let your user start and stop the CML VM, then **log out and back in once**:

```bash
sudo usermod -aG libvirt $USER
```

### 2. Sign in to GitHub (one time)

```bash
gh auth login
```

Answer the questions like this:

| Question | Choose |
|---|---|
| Where do you use GitHub? | `GitHub.com` |
| Preferred protocol | `HTTPS` |
| Authenticate Git with your GitHub credentials? | `Yes` |
| How would you like to authenticate? | `Login with a web browser` |

It prints a one-time code. Press Enter, paste the code in the browser page that opens, and approve.

### 3. Download LabDesk

```bash
cd ~
git clone https://github.com/eTheDestroyer/labdesk.git
cd labdesk
```

You now have the folder `~/labdesk` with the program in it.

### 4. Install it

```bash
./install.sh
```

The installer checks the packages, puts LabDesk's Python packages into a private folder (`~/labdesk/.venv`),
asks for your CML login once, and adds LabDesk to the application menu:

```
CML address [192.168.122.60]:    ← press Enter if that is your CML's address
CML username [admin]:            ← press Enter for admin
CML password:                    ← type it; nothing shows while you type, that is normal
```

The login is saved in `~/cml-labs/cml-credentials.env`. Only you can read that file, and it is outside the git
folder, so it is never uploaded.

### 5. Open LabDesk

Either open the **application menu** and type **LabDesk**, or run:

```bash
~/labdesk/labdesk
```

If the status bar says **CML offline**, click **Start CML VM** and wait about 2 minutes.

---

## Windows

For Windows 10 and 11 (64-bit). Commands go in **PowerShell**: press the Windows key, type `powershell`, press Enter.

> **Don't need git?** You can skip all of this: download **LabDesk.exe** from the repository's *Releases* page and
> double-click it ([INSTALL.md → LabDesk for Windows](INSTALL.md#labdesk-for-windows)). Use the git way below when
> you want the newest version straight from the repository.

### 1. Install Git and Python

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.13 -e
```

**Close PowerShell and open it again** so the two new programs are found. Check:

```powershell
git --version
py --version
```

Both must print a version number (Python 3.11 or newer).

> No `winget`? Download the installers instead: Git from `git-scm.com/download/win`, Python from
> `python.org/downloads` (keep the default options).
>
> `py --version` says *not recognized*? Python is not installed yet, or PowerShell was not reopened.

### 2. Download LabDesk and sign in to GitHub

```powershell
cd $HOME
git clone https://github.com/eTheDestroyer/labdesk.git
cd labdesk
```

The first time, a **Connect to GitHub** window opens. Click **Sign in with your browser** and approve.
Git remembers the sign-in after that.

You now have the folder `C:\Users\<you>\labdesk` with the program in it.

### 3. Install it

```powershell
.\install.bat
```

(Or double-click `install.bat` in the `labdesk` folder.) It creates a private Python folder (`labdesk\.venv`)
and installs LabDesk's Python packages into it. The first time takes a few minutes. Nothing is installed
system-wide.

### 4. Open LabDesk

Double-click **`labdesk.bat`** in the `labdesk` folder, or run:

```powershell
.\labdesk.bat
```

To get a Desktop icon: right-click `labdesk.bat` → **Send to** → **Desktop (create shortcut)**.

With the LabDesk-VMs folder in place nothing is asked. Without it, the first time LabDesk asks where CML is:

```
CML address:   192.168.80.130    ← the address you open in the browser to reach CML
Username:      admin
Password:      ••••••            ← the CML password
```

Click **Test connection**. When it says **✓ Connected**, click **Save**.
The login is saved in `%APPDATA%\LabDesk\cml-credentials.env`, outside the git folder.

### Click and play: the VMs (CML, vJunos, FortiOS, Internet cloud)

Build the **LabDesk-VMs** folder once on the Linux PC (`.venv/bin/python tools/make_windows_bundle.py`), copy it
into `C:\Users\<you>\labdesk\LabDesk-VMs` and start `labdesk.bat`: LabDesk installs / uses VMware Workstation,
creates its network (one administrator prompt) and starts CML by itself - no connection dialog.
Details: [INSTALL.md → LabDesk for Windows](INSTALL.md#labdesk-for-windows).

---

## Getting a newer version later

**Linux**

```bash
cd ~/labdesk
git pull
./install.sh          # quick: it keeps your saved login
```

**Windows**

```powershell
cd $HOME\labdesk
git pull
.\install.bat
```

Then close LabDesk and open it again.

## If something goes wrong

| What you see | System | What to do |
|---|---|---|
| `Repository not found` or `Authentication failed` on `git clone` | both | You are not signed in, or your GitHub account has no access to the repository. Linux: `gh auth login` again. Windows: run `git clone` again and sign in when the window opens |
| `git: command not found` / `git is not recognized` | both | Git is not installed, or (Windows) PowerShell was not reopened after installing it |
| `./install.sh: Permission denied` | Linux | `chmod +x install.sh labdesk`, then run it again |
| The installer says *Missing packages* | Linux | Run the `sudo apt install …` line it prints, then `./install.sh` again |
| `Python 3.11 or newer was not found` | Windows | Install Python (step 1), reopen PowerShell, run `.\install.bat` again |
| `LabDesk is not installed in this folder yet` | Windows | Run `install.bat` first |
| `git pull` says *Your local changes would be overwritten* | both | You edited a file in the folder. `git stash`, then `git pull` |
| **CML offline** in the status bar | both | Is the CML VM running? Can you open `https://<CML address>` in a browser on this PC? |

More help: [INSTALL.md](INSTALL.md).

## Removing LabDesk

**Linux**

```bash
cd ~/labdesk
./install.sh --uninstall      # removes the menu entry and the .venv folder
cd ~ && rm -rf ~/labdesk      # removes the program itself
```

**Windows:** delete the `labdesk` folder. To also remove your saved login and settings, delete
`%APPDATA%\LabDesk` and `%LOCALAPPDATA%\LabDesk`.
