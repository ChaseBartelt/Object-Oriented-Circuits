"""Launch the IDE viewer or optional source editor."""

import argparse
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="Circuit source file")
    parser.add_argument("--editor", action="store_true",
                        help="Show the IDE viewer and accept reload requests on stdin")
    args = parser.parse_args(argv)
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    app = QApplication([sys.argv[0]])
    if args.editor:
        from circuitlab.tooling.editor_bridge import EditorBridge
        from circuitlab.viewer.window import ViewerWindow

        window = ViewerWindow()
        bridge = EditorBridge(window)
        bridge.requested.connect(window.open_from_editor, Qt.ConnectionType.QueuedConnection)
        bridge.refreshed.connect(window.refresh_from_editor, Qt.ConnectionType.QueuedConnection)
    else:
        from circuitlab.viewer.legacy import MainWindow
        window = MainWindow()
    if args.file:
        window.open_path(args.file)
    window.show()
    if args.editor:
        bridge.start(sys.stdin)
    return app.exec()


if __name__ == "__main__":
    if not __package__:
        from pathlib import Path

        # Replace the script directory so local modules cannot shadow the standard library.
        script_dir = Path(__file__).resolve().parent
        if sys.path and Path(sys.path[0]).resolve() == script_dir:
            sys.path.pop(0)
        sys.path.insert(0, str(script_dir.parent))

    raise SystemExit(main())
