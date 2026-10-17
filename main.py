"""Primary entry point for source runs and packaged Windows builds."""

from multiprocessing import freeze_support


def main():
    from checkin.cli import main as run

    return run()


if __name__ == "__main__":
    freeze_support()
    raise SystemExit(main())
