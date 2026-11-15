"""Launch the IDE viewer or optional source editor without loading both.

Historical imports such as ``from circuitlab.gui import MainWindow`` remain
available lazily. IDE launches neither import nor construct the source editor.
"""

import argparse
import sys


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)
    if name == "ViewerWindow":
        from circuitlab.viewer.window import ViewerWindow
        return ViewerWindow
    from circuitlab.viewer import legacy
    return getattr(legacy, name)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Open the OOC viewer or optional source editor.")
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
    raise SystemExit(main())
