@echo off
setlocal

cd /d %~dp0

if not exist backend (
  echo backend folder not found.
  exit /b 1
)

cd backend

if not exist .venv (
  echo Creating venv...
  py -m venv .venv
)

call .venv\Scripts\activate.bat

python -m pip install --upgrade pip
pip install -r requirements.txt

echo Starting FastAPI on http://localhost:8000
python -m uvicorn app:app --reload --host 0.0.0.0 --port 8000

endlocal
