"""Launch Electric Circuits from the terminal or an editor."""

if __name__ == "__main__":
    if not __package__:
        import sys
        from pathlib import Path

        # Replace the script directory so local modules cannot shadow the standard library.
        script_dir = Path(__file__).resolve().parent
        if sys.path and Path(sys.path[0]).resolve() == script_dir:
            sys.path.pop(0)
        sys.path.insert(0, str(script_dir.parent))

    from circuitlab.gui import main

    raise SystemExit(main())
