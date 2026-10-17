"""Entry point for PyInstaller and `python app/run.py`."""
import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from sigmadesk.main import main
    sys.exit(main())
