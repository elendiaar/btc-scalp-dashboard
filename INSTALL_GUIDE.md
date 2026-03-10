# BTC Scalp Dashboard — Installation Guide (Windows)

## Step 1: Open Command Prompt

Press **Win + R**, type `cmd`, press **Enter**.

---

## Step 2: Verify Python is installed

Type this and press Enter:

```
python --version
```

You should see something like `Python 3.13.x` or `Python 3.14.x`.

If you see an error ("not recognized"), reinstall Python from https://www.python.org/downloads/ — make sure to check **"Add Python to PATH"** during installation.

---

## Step 3: Download the project

Type these commands one at a time, pressing Enter after each:

```
cd %USERPROFILE%\Desktop
git clone https://github.com/elendiaar/btc-scalp-dashboard.git
cd btc-scalp-dashboard
```

If you see `'git' is not recognized`, download and install Git from https://git-scm.com/download/win — then close and reopen Command Prompt and try again.

**Alternative (no Git):** Go to https://github.com/elendiaar/btc-scalp-dashboard, click the green **Code** button, then **Download ZIP**. Extract the ZIP to your Desktop. Then in Command Prompt:

```
cd %USERPROFILE%\Desktop\btc-scalp-dashboard-master
```

---

## Step 4: Install dependencies

Type this and press Enter:

```
pip install -r requirements.txt
```

Wait until it finishes. You should see "Successfully installed" messages.

---

## Step 5: Start the dashboard

Type this and press Enter:

```
python server.py
```

You should see:

```
INFO:     Uvicorn running on http://0.0.0.0:5000 (Press CTRL+C to quit)
```

---

## Step 6: Open the dashboard

Open your web browser (Chrome, Edge, Firefox) and go to:

```
http://localhost:5000
```

The dashboard will load and start showing live BTC data within a few seconds.

---

## Stopping the dashboard

Go back to the Command Prompt window where the server is running and press **Ctrl + C**.

---

## Starting it again later

Open Command Prompt, then:

```
cd %USERPROFILE%\Desktop\btc-scalp-dashboard
python server.py
```

Then open http://localhost:5000 in your browser.

---

## Upgrading to Binance Futures data (better data for scalping)

Since you're in Romania (not geo-restricted by Binance), you can switch to the Binance Futures API for real funding rates, open interest, and liquidation data.

Open `server.py` in any text editor (Notepad, VS Code, etc.) and find-and-replace:

| Find this | Replace with |
|---|---|
| `api.binance.us/api/v3` | `fapi.binance.com/fapi/v1` |

Save the file, stop the server (Ctrl+C), and start it again (`python server.py`).

---

## Troubleshooting

**"pip is not recognized"** — Try `python -m pip install -r requirements.txt` instead.

**"Port 5000 already in use"** — Another program is using that port. Close it, or edit `server.py` and change the last line from `port=5000` to `port=8080`, then open `http://localhost:8080` instead.

**Dashboard shows but no data** — Check that your internet connection works and that Binance is not blocked by your network. The Command Prompt window will show error messages if data fetching fails.

**Windows Firewall popup** — Click "Allow access" when Windows asks about Python accessing the network.
