"""IDE-first schematic/results window; source editing belongs to the IDE."""
# The IDE-first shell owns a session and presentation state rather than a source text widget.
# Render/solve results, layout comments, and value edits must follow the same accepted source
# revision; background refreshes must not steal focus or discard unacknowledged edits.

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QSettings, QSignalBlocker, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel,
    QListWidgetItem, QMainWindow, QMessageBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from circuitlab.exporters.ltspice import export_hierarchy, export_schematic
from circuitlab.language.linked import CHIP_LIBRARY
from circuitlab.language.syntax import has_chip
from circuitlab.layout.devices import DeviceLayouts
from circuitlab.layout.hierarchy import hood_levels
from circuitlab.layout.persistence import (
    read_pin_overrides, read_placement, read_logic_placement, embed_placement,
)
from circuitlab.math.analysis import Diagnostic
from circuitlab.math.digital import DigitalSimulation
from circuitlab.math.report import pin_rows, result_rows
from circuitlab.model.graph import CircuitError
from circuitlab.tooling.session import CircuitSession, includes_files, issue_for
from circuitlab.tooling.source_edits import SourceEdit
from circuitlab.viewer.exports import export_file, hierarchy_layouts
from circuitlab.viewer.device_layouts import DeviceLayoutDialog
from circuitlab.viewer.digital import DigitalSimulator
from circuitlab.viewer.schematic import SchematicView, build_schematic_layout
from circuitlab.viewer.scope import ScopeSidebar, ScopeToggle
from circuitlab.viewer.source_editing import SourceTools
from circuitlab.viewer.widgets import (
    DiagnosticsList, GateLayoutDialog, MODES, Results, STYLESHEET, SchematicHeader,
    button, grid_button, probe_text, search_text,
)


def prepare_digital_preview(source, mode, profile, path=None, read_source=None):
    """Compile, route, and initialize a digital preview without touching widgets."""
    session = CircuitSession(source=source, path=path, read_source=read_source)
    session.compile()
    if session.drawing is None:
        error = session.issues[0].message if session.issues else None
        return session, None, None, error
    _, view, _, _ = next(hood_levels(session.drawing, set()))
    layout = build_schematic_layout(view, mode)
    if session.interface_preview:
        return session, layout, None, None
    try:
        simulation = DigitalSimulation(source, profile=profile, design=session.digital_design)
    except (CircuitError, ValueError) as error:
        return session, layout, None, str(error)
    return session, layout, simulation, None


class ViewerWindow(QMainWindow):
    """Present one accepted source document without constructing a text editor.

    Layout comments and value edits are saved through the IDE when attached,
    or through checked file writes when running standalone.
    """

    def __init__(self, session=None):
        super().__init__()
        self.session = session or CircuitSession()
        self.layout_issues = []
        self._source_issues = []
        self._preview_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="digital-preview")
        worker = self._preview_worker
        self.destroyed.connect(lambda: worker.shutdown(wait=False, cancel_futures=True))
        self._preview_job = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setInterval(30)
        self._preview_timer.timeout.connect(self._finish_digital_preview)
        self.placement_dirty = False
        self.resize(1100, 780)
        self.setStyleSheet(STYLESHEET)
        central = QWidget()
        self.setCentralWidget(central)
        panel = QVBoxLayout(central)
        controls = QHBoxLayout()
        self.mode = QComboBox()
        for key, label in MODES.items():
            self.mode.addItem(label, key)
        controls.addWidget(self.mode)
        button("Compile / Solve", self.compile_source, controls).setObjectName("compile")
        button("Render schematic", self.render_source, controls)
        button("Save layout", self.save, controls)
        button("Analysis window", self.show_analysis, controls)
        button("Export to SPICE", self.export_netlist, controls)
        button("FPGA…", self.show_fpga, controls)
        controls.addStretch()
        panel.addLayout(controls)
        self.views = QTabWidget()
        panel.addWidget(self.views, 1)
        self.views.addTab(self._create_schematic_page(), "Schematic")
        self.results = Results()
        self.views.addTab(self.results, "Results")
        self.digital = DigitalSimulator()
        self.digital.values_changed.connect(self.update_logic_colors)
        self.schematic.logic_input_requested.connect(self.digital.toggle_input)
        self.digital.port_view.placement_changed.connect(self.placement_changed)
        self.digital_tab = self.views.addTab(self.digital, "Logic simulation")
        self.views.setTabVisible(self.digital_tab, False)
        self.issues = DiagnosticsList()
        self.issues.itemDoubleClicked.connect(self.select_diagnostic)
        panel.addWidget(self.issues)
        self.status = QLabel("Open a circuit from your IDE or File → Open.")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        panel.addWidget(self.status)
        self.analysis_window = QDialog(self)
        self.analysis_window.setWindowTitle("Circuit analysis")
        self.analysis_window.resize(900, 560)
        self.analysis_results = Results()
        QVBoxLayout(self.analysis_window).addWidget(self.analysis_results)
        self._create_menus()
        self.mode.currentIndexChanged.connect(self.compile_source)
        self.update_title()
        self.show_issues([])
        self.views.currentChanged.connect(self.update_diagnostics_visibility)
        self.update_diagnostics_visibility()

    def update_diagnostics_visibility(self, *_):
        self.issues.setVisible(self.views.currentWidget() is not self.digital)

    @property
    def path(self):
        return self.session.path

    @property
    def solution(self):
        return self.session.solution

    @property
    def pins(self):
        return self.session.pins

    def read_linked_source(self, path):
        return self.session.read_linked_source(path)

    def _create_schematic_page(self):
        page = QWidget()
        canvas = QVBoxLayout(page)
        canvas.setContentsMargins(0, 0, 0, 0)
        settings = QSettings("Electric Circuits", "Object-Oriented Circuits")
        self.schematic = SchematicView(DeviceLayouts(settings))
        self.schematic.device_layout_requested.connect(self.edit_device_layouts)
        self.schematic.gate_layout_requested.connect(
            lambda name, inputs: self.edit_gate_layouts((name, inputs))
        )
        self.schematic.placement_changed.connect(self.placement_changed)
        self.schematic.layout_issues_changed.connect(self.update_layout_issues)
        self.schematic.probed.connect(self.probe)
        self.schematic.pin_requested.connect(self.name_pin)
        self.schematic.pin_renamed.connect(self.name_pin)
        self.schematic.pin_removed.connect(lambda net: self.write_pin(net, None))
        self.scope_sidebar = ScopeSidebar(self.schematic)
        self.scope = self.scope_sidebar.scope
        self.scope_toggle = ScopeToggle(self.views, self.scope_sidebar)
        self.schematic_header = SchematicHeader(self.views, self.scope_toggle)
        controls = self.schematic_header.primary
        self.wire_style = QComboBox()
        for label, mode, tip in (
            ("Auto layout", "auto", "Arrange parts with continuous wiring."),
            ("Labeled connectors", "labeled", "Matching names connect distant terminals."),
            ("All wires", "routed", "Start with continuous wires."),
        ):
            self.wire_style.addItem(label, mode)
            self.wire_style.setItemData(self.wire_style.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
        self.wire_style.setAccessibleName("Schematic wiring style")
        self.wire_style.currentIndexChanged.connect(self.change_wiring)
        controls.addWidget(self.wire_style)
        button("Reset placement", self.schematic.reset_placement, controls).setToolTip(
            "Restore the automatic layout. Press Ctrl+Z in the schematic to undo a move."
        )
        self.pin_button = button("Add pin", self.toggle_pin_tool, controls)
        self.pin_button.setCheckable(True)
        grid_button(self.schematic, controls, alignment_layout=self.schematic_header.secondary)
        self.schematic_step = button("Step logic", lambda: self.digital.step(), controls)
        self.schematic_step.setObjectName("schematic-step")
        self.schematic_step.setToolTip("Advance one logic clock cycle while viewing the schematic.")
        self.schematic_step.setEnabled(False)
        self.schematic_step.hide()
        controls = self.schematic_header.secondary
        button("Zoom in", lambda: self.schematic.zoom(1.4), controls)
        button("Zoom out", lambda: self.schematic.zoom(1 / 1.4), controls)
        button("Fit", self.schematic.fit, controls)
        self.source_tools = SourceTools(self, controls)
        button("Export LTspice .asc", self.export_ltspice, controls)
        canvas.addWidget(self.scope_sidebar, 1)
        return page

    def _create_menus(self):
        files = self.menuBar().addMenu("&File")
        files.addAction("&Open…", self.pick_file, QKeySequence.StandardKey.Open)
        files.addAction("&Save layout", self.save, QKeySequence.StandardKey.Save)
        files.addAction("&Reload from disk", self.reload_file)
        files.addSeparator()
        files.addAction("Export to &SPICE…", self.export_netlist)
        files.addAction("Export LTspice &Schematic…", self.export_ltspice)
        files.addAction("Export &Verilog…", self.export_verilog)
        files.addSeparator()
        files.addAction("&Quit", self.close, QKeySequence.StandardKey.Quit)
        view = self.menuBar().addMenu("&View")
        self.scope_action = self.scope_toggle.defaultAction()
        view.addAction(self.scope_action)
        view.addAction("Chip Library…", self.show_chip_library)
        view.addAction("Gate Layouts…", lambda: self.edit_gate_layouts())
        view.addAction("Chip & Circuit Layouts…", lambda: self.edit_device_layouts())
        view.addAction("Optimize wiring", self.schematic.optimize_wiring)
        compile_action = self.menuBar().addMenu("&Circuit").addAction("&Compile / Solve", self.compile_source)
        compile_action.setShortcuts([QKeySequence("Ctrl+Return"), QKeySequence("Ctrl+Enter")])

    def open_from_editor(self, filename, *, activate=True):
        if self.source_tools.pending:
            return False
        if not activate and self.placement_dirty:
            self.status.setText("Auto-refresh paused: save your layout, then save in your IDE again.")
            return False
        if not self.confirm_edits():
            return False
        preserve_scope = not activate and Path(filename).resolve() == self.path
        if not self.open_path(filename, show_errors=activate, preserve_scope=preserve_scope):
            return False
        if activate:
            if self.isMinimized():
                self.showNormal()
            self.show()
            self.raise_()
            self.activateWindow()
        return True

    # Treat background reload as synchronization, not a user request to raise the window
    # or discard pending source/value edits.
    def refresh_from_editor(self, filename):
        return self.open_from_editor(filename, activate=False)

    def open_path(self, filename, *, show_errors=True, preserve_scope=False):
        """Load an accepted file; callers confirm dirty layout before calling."""
        try:
            self.session.load(filename)
        except (OSError, UnicodeError) as error:
            self.status.setText(str(error))
            if show_errors:
                QMessageBox.warning(self, "Open circuit", str(error))
            return False
        self.session.pin_overrides = read_pin_overrides(self.path)
        mode, records = read_placement(self.path)
        self.load_placement(mode, records)
        logic = read_logic_placement(self.path)
        self.schematic.load_logic_placements(logic.get('schematic', {}))
        self.digital.load_logic_placements(logic.get('simulation', {}))
        self.update_title()
        self.compile_source(preserve_scope=preserve_scope)
        return True

    def pick_file(self):
        name, _ = QFileDialog.getOpenFileName(
            self, "Open circuit", str(self.path.parent if self.path else Path.cwd()),
            "Circuit files (*.ckt *.txt);;All files (*)",
        )
        if name:
            self.open_from_editor(name)

    def reload_file(self):
        if self.path:
            return self.open_from_editor(self.path)
        return False

    def show_chip_library(self):
        name, _ = QFileDialog.getOpenFileName(
            self, "Open chip reference", str(CHIP_LIBRARY), "Circuit files (*.ckt)",
        )
        if name:
            self.open_from_editor(name)

    def edit_gate_layouts(self, initial=None):
        dialog = GateLayoutDialog(self, self.schematic.gate_layouts, initial)
        dialog.exec()
        dialog.deleteLater()

    def edit_device_layouts(self, initial=None):
        dialog = DeviceLayoutDialog(self, self.schematic.gate_layouts, initial)
        dialog.exec()
        dialog.deleteLater()

    # Refresh session results and diagnostics from the current accepted source revision.
    def compile_source(self, *, preserve_scope=False):
        show_logic = self.views.currentWidget() is self.digital
        self._cancel_digital_preview()
        if getattr(self, "fpga_window", None):
            self.fpga_window.refresh_source()
        if not preserve_scope:
            self.scope_sidebar.set_circuit(None)
        self.schematic.clear(preserve_placement=True)
        if self._queue_digital_preview(show_logic, preserve_scope):
            return
        self.session.compile(self.mode.currentData())
        self._show_compilation(show_logic, preserve_scope)

    def _show_compilation(self, show_logic, preserve_scope, *, layout=None, prepared=None):
        self.update_results()
        if self.session.drawing is not None:
            try:
                self.schematic.draw(self.session.drawing, self.pins, layout=layout)
            except CircuitError as error:
                self.schematic.clear(preserve_placement=True)
                self.session.issues.append(issue_for(error))
            self.scope_sidebar.set_circuit(self.session.drawing, preserve_state=preserve_scope)
        elif preserve_scope:
            self.scope_sidebar.set_circuit(None)
        if prepared is None or self.session.interface_preview:
            self.update_digital()
        else:
            self.digital.set_prepared_source(
                self.session.source, *prepared, design=self.session.digital_design,
            )
            self.views.setTabVisible(self.digital_tab, True)
        self.pin_button.setChecked(False)
        self.schematic.set_pin_tool(False)
        self.show_issues(self.session.issues)
        if self.visible_issues:
            self.status.setText(self.visible_issues[0].message)
        elif self.session.interface_preview:
            self.views.setCurrentIndex(0)
            self.status.setText("Chip interface preview. Include this file in a circuit with main to compile and simulate it.")
        elif self.session.preview_only:
            self.views.setCurrentIndex(self.digital_tab if show_logic and self.views.isTabVisible(self.digital_tab) else 0)
            self.status.setText(
                "Open Logic simulation to toggle inputs, step the clock, and inspect signals. "
                "Wire colors: green = 1, gray = 0; blue = selected."
                if self.views.isTabVisible(self.digital_tab) else "Schematic rendered."
            )
        elif self.solution and self.solution.network:
            self.status.setText(
                f"{MODES[self.solution.circuit.mode]} · Click a wire to trace it or add a named pin."
            )
        elif self.solution:
            self.views.setCurrentWidget(self.results)
            self.status.setText("Results updated.")
        else:
            self.status.setText("Open a circuit from your IDE or File → Open.")

    def _queue_digital_preview(self, show_logic, preserve_scope):
        source = self.session.source
        # Keep small previews immediate. This threshold only schedules work.
        try:
            if (len(source) < 64 * 1024 and not includes_files(source)) or not self.session.is_digital():
                return False
        except CircuitError:
            return False
        mode = self.schematic.layout_mode
        future = self._preview_worker.submit(
            prepare_digital_preview, source, mode, self.digital.selected_profile(),
            self.session.path, self.session.read_source,
        )
        self._preview_job = future, self.session.revision, mode, show_logic, preserve_scope
        self.session.solution = None
        self.session.drawing = None
        self.session.issues = []
        self.session.digital_design = None
        self.session.interface_preview = False
        self.update_results()
        self.show_issues([])
        self.digital.set_source("")
        self.digital.profile.setEnabled(False)
        self.status.setText("Preparing digital schematic and simulation…")
        self._preview_timer.start()
        return True

    def _cancel_digital_preview(self):
        if self._preview_job is not None:
            self._preview_job[0].cancel()
            self._preview_job = None
        self._preview_timer.stop()
        self.digital.profile.setEnabled(True)

    def _finish_digital_preview(self):
        if self._preview_job is None:
            return
        future, revision, mode, show_logic, preserve_scope = self._preview_job
        if not future.done():
            return
        self._cancel_digital_preview()
        if revision != self.session.revision:
            return
        try:
            session, layout, sim, error = future.result()
        except CircuitError as error:
            self.show_issues([issue_for(error)])
            self.status.setText(error.message)
            return
        self.session.solution = session.solution
        self.session.drawing = session.drawing
        self.session.issues = session.issues
        self.session.preview_only = session.preview_only
        self.session.interface_preview = session.interface_preview
        self.session.digital_design = session.digital_design
        self._show_compilation(
            show_logic, preserve_scope, layout=layout if mode == self.schematic.layout_mode else None,
            prepared=(sim, error),
        )

    def render_source(self):
        self._cancel_digital_preview()
        if self._queue_digital_preview(False, False):
            self.schematic.clear(preserve_placement=True)
            self.scope_sidebar.set_circuit(None)
            self.views.setCurrentIndex(0)
            return True
        try:
            circuit = self.session.render(self.mode.currentData())
            self.schematic.draw(circuit, self.pins)
        except CircuitError as error:
            self.schematic.clear(preserve_placement=True)
            self.scope_sidebar.set_circuit(None)
            self.digital.set_source("")
            self.views.setTabVisible(self.digital_tab, False)
            self.show_issues([issue_for(error)])
            self.status.setText(error.message)
            return False
        self.scope_sidebar.set_circuit(circuit)
        self.update_digital()
        self.views.setCurrentIndex(0)
        self.show_issues(self.session.issues)
        message = ("Chip interface preview. Include this file in a circuit with main to compile and simulate it."
                   if self.session.interface_preview else "Schematic rendered.")
        self.status.setText(self.layout_issues[0].message if self.layout_issues else message)
        return True

    # Digital software runtime state is separate from an analog chip's electrical model preview;
    # switching source kinds must clear values that no longer belong to the current circuit.
    def update_digital(self):
        """Keep the digital runtime separate from electrical model previews."""
        design = self.session.digital_design
        digital = not self.session.interface_preview and (
            design is not None or self.session.preview_only and has_chip(self.session.source)
        )
        if digital and design is None and self.session.issues:
            self.digital.set_prepared_source(self.session.source, None, self.session.issues[0].message)
        else:
            self.digital.set_source(self.session.source if digital else "", design=design)
        self.views.setTabVisible(self.digital_tab, digital)

    def update_logic_colors(self, values):
        # Samples are taken between complete cycles. Leave clock wires neutral
        # instead of presenting their sampled low phase as a constant logic 0.
        sim = self.digital.simulation
        can_step = bool(sim and sim.clocks)
        if self.schematic_step.isEnabled() != can_step:
            self.schematic_step.setEnabled(can_step)
            self.schematic_step.setVisible(can_step)
            self.schematic_header.show_tools(self.views.currentIndex())
        self.schematic.set_logic_simulation(sim, values)
        clocks = sim.clocks if sim else ()
        self.schematic.set_logic_values({name: value for name, value in values.items()
                                         if name not in clocks and (sim is None or sim.signals.get(name) == 1)})

    def update_results(self):
        rows = result_rows(self.solution) if self.solution else []
        rows = pin_rows(self.solution, self.pins) + rows
        self.results.update_rows(rows)
        self.analysis_results.update_rows(rows)

    @property
    def visible_issues(self):
        return [*self._source_issues, *self.layout_issues]

    def update_layout_issues(self, issues):
        """Keep placement warnings live without replacing compiler diagnostics."""
        had_warnings = bool(self.layout_issues)
        self.layout_issues = [Diagnostic("warning", issue.line, issue.message, issue.path) for issue in issues]
        self.show_issues(self._source_issues)
        if self.visible_issues:
            self.status.setText(self.visible_issues[0].message)
        elif had_warnings:
            self.status.setText("Schematic rendered.")

    def show_issues(self, issues):
        self._source_issues = list(issues)
        issues = self.visible_issues
        self.issues.clear()
        if not issues:
            item = QListWidgetItem("No compiler diagnostics.")
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.issues.addItem(item)
        for issue in issues:
            location = f"{issue.path.name} · " if issue.path and issue.path != self.path else ""
            label = f"Line {issue.line}" if issue.line is not None else "Circuit"
            item = QListWidgetItem(f"{location}{label} · {issue.severity} · {issue.message}")
            item.setForeground(QColor("#f87171" if issue.severity == "error" else "#ffd18b"))
            item.setData(Qt.ItemDataRole.UserRole, issue.line)
            item.setData(Qt.ItemDataRole.UserRole + 1, str(issue.path or self.path or ""))
            item.setToolTip(issue.message + "\nDouble-click to open in VS Code.")
            self.issues.addItem(item)
        self.issues.fit_rows(status=not issues)

    def select_diagnostic(self, item):
        path = item.data(Qt.ItemDataRole.UserRole + 1)
        line = item.data(Qt.ItemDataRole.UserRole)
        if path:
            target = QUrl()
            target.setScheme("vscode")
            target.setHost("file")
            target.setPath("/" + Path(path).as_posix() + (f":{line}" if line else ""))
            QDesktopServices.openUrl(target)

    def show_analysis(self):
        self.analysis_window.show()
        self.analysis_window.raise_()
        self.analysis_window.activateWindow()

    def probe(self, kind, name):
        simulation = self.digital.simulation
        if simulation is not None and kind in {"node", "pin"} and name in simulation.values:
            if name in simulation.clocks:
                rate = f" ({simulation.frequency:g} Hz)" if simulation.frequency else ""
                self.status.setText(f"{name}: simulated clock{rate} · Step advances one complete cycle.")
                return
            self.digital.probe.setCurrentText(name)
            self.status.setText(f"{name} = {simulation.values[name]} · simulated logic level")
            return
        if kind == "pin" and name in self.pins:
            self.results.search.setText(self.pins[name])
            self.views.setCurrentWidget(self.results)
            kind = "node"
        self.status.setText(probe_text(self.solution, kind, name))

    def toggle_pin_tool(self, active):
        if active and not self.schematic.circuit:
            self.pin_button.setChecked(False)
            self.status.setText("Compile or render a circuit before adding a pin.")
            return
        self.schematic.set_pin_tool(active)
        self.status.setText(
            "Click a wire, connector, or component terminal to place a pin."
            if active else "Click a wire to trace it or add a named pin."
        )

    def name_pin(self, net):
        self.pin_button.setChecked(False)
        self.schematic.set_pin_tool(False)
        existing = self.pins.get(net)
        name, accepted = QInputDialog.getText(
            self, "Rename pin" if existing else "Add pin", f"Name for the pin on {net}",
            text=existing or net,
        )
        name = " ".join(name.split())
        if not accepted or not name:
            return
        if any(key != net and search_text(other) == search_text(name) for key, other in self.pins.items()):
            QMessageBox.warning(self, "Pin name in use", "Choose a different name for this pin.")
            return
        self.write_pin(net, name)
        self.probe("pin", net)

    def write_pin(self, net, name):
        previous = self.pins.get(net)
        self.session.pin_overrides[net] = name
        self.schematic.update_pins(self.pins, fit=False)
        self.update_results()
        self.placement_changed()
        if name is None:
            if search_text(self.results.search.text()) == search_text(previous or ""):
                self.results.search.clear()
            self.status.setText("Pin removed. Save layout to keep this change.")
        else:
            self.status.setText("Pin updated. Save layout to keep this change.")

    def change_wiring(self):
        self.schematic.layout_mode = self.wire_style.currentData()
        if self.schematic.circuit:
            selected = self.schematic.highlight
            self.schematic.redraw(self.pins)
            if selected:
                self.schematic.highlight_net(selected)
            self.placement_changed()

    def load_placement(self, mode, records):
        self.schematic.clear()
        with QSignalBlocker(self.wire_style):
            index = self.wire_style.findData(mode if isinstance(mode, str) else "auto")
            self.wire_style.setCurrentIndex(max(0, index))
        self.schematic.layout_mode = self.wire_style.currentData()
        self.schematic.placement_records = records
        self.placement_dirty = False
        self.update_title()

    def placement_changed(self):
        self.placement_dirty = True
        self.update_title()

    def logic_placements(self):
        return {'schematic': self.schematic.logic_placements(),
                'simulation': self.digital.logic_placements()}

    def update_title(self):
        name = self.path.name if self.path else "OOC Viewer"
        self.setWindowTitle(f"{name}[*] — Electric Circuits")
        self.setWindowModified(self.placement_dirty)

    # Read the latest source before embedding presentation metadata. In IDE mode, retain dirty
    # state until the checked edit is acknowledged instead of overwriting the editor's text.
    def save(self):
        """Save layout comments against the latest source, through the IDE when attached."""
        if self.path is None:
            self.status.setText("Open a saved circuit before saving its layout.")
            return False
        if self.source_tools.pending:
            self.status.setText('Wait for the current source edit before saving the layout.')
            return False
        try:
            before = self.path.read_bytes().decode('utf-8-sig')
            after = embed_placement(before, self.schematic.layout_mode,
                                    self.schematic.placements(), pin_overrides=self.session.pin_overrides,
                                    logic_layouts=self.logic_placements())
            plan = SourceEdit(self.path, before, after, 'Save schematic layout')
            if not self.source_tools.write_and_wait(plan):
                return False
            previous = self.source_tools.current_identity()
            self.session.disk_bytes = self.path.read_bytes()
            self.session.source = self.session.disk_bytes.decode('utf-8-sig')
            if self.source_tools.current_identity() != previous:
                self.compile_source()
            self.source_tools.identity = self.source_tools.current_identity()
            self.source_tools.checkpoint()
            self.placement_dirty = False
            self.update_title()
            self.status.setText(f'Saved layout and values in {self.path.name}.')
            return True
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, 'Save schematic layout', str(error))
            return False

    def confirm_edits(self):
        if self.source_tools.pending:
            self.status.setText('Wait for the source edit to finish before closing or switching files.')
            return False
        if not self.placement_dirty and not self.source_tools.value_changes:
            return True
        options = QMessageBox.StandardButton
        choice = QMessageBox.question(
            self, "Save viewer changes", f"Save layout and value changes to {self.path.name if self.path else 'this circuit'}? Discard restores unsaved value changes in the code.",
            options.Save | options.Discard | options.Cancel, options.Save,
        )
        return self.source_tools.discard_values() if choice == options.Discard else (choice == options.Save and self.save())

    def export_netlist(self):
        return export_file(
            self, lambda: self.session.spice(self.mode.currentData()),
            "Export to SPICE", ".cir", "SPICE netlists (*.cir)",
        )

    def export_verilog(self):
        return export_file(
            self, self.session.verilog, "Export Verilog", ".v", "Verilog source (*.v)",
            next_step="Add it to your FPGA project.",
        )

    def show_fpga(self):
        from circuitlab.viewer.fpga import FpgaDialog
        if not getattr(self, "fpga_window", None):
            self.fpga_window = FpgaDialog(self, lambda: (self.path, self.session.source))
        self.fpga_window.refresh_source()
        self.fpga_window.show()
        self.fpga_window.raise_()
        self.fpga_window.activateWindow()

    def export_ltspice(self):
        used_labels = False
        block_files = {}

        def schematic_text():
            nonlocal used_labels, block_files
            if has_chip(self.session.source):
                raise CircuitError("Use File > Export Verilog for this chip.")
            if self.schematic.layout is None or includes_files(self.session.source):
                circuit = self.session.render(self.mode.currentData())
                self.schematic.draw(circuit, self.pins)
                self.scope_sidebar.set_circuit(circuit)
                self.views.setCurrentIndex(0)
            circuit, drawing = self.schematic.circuit, self.schematic.layout
            if drawing is None:
                raise CircuitError("Enter components before exporting an LTspice schematic.")
            if drawing.uses_packages:
                details = hierarchy_layouts(self.schematic)
                text, block_files, used_labels = export_hierarchy(circuit, drawing, details)
            else:
                text, used_labels = export_schematic(circuit, drawing)
            return text

        exported = export_file(
            self, schematic_text, "Export LTspice Schematic", ".asc", "LTspice schematics (*.asc)",
            companions=lambda: block_files,
        )
        if exported:
            notes = []
            if block_files:
                notes.append("Keep the exported block sheets and symbols beside the main schematic.")
            if used_labels:
                notes.append("Matching net labels connect the crowded wires.")
            if notes:
                self.status.setText(" ".join((self.status.text(), *notes)))
        return exported

    # Respect outstanding edit transactions and stop live simulation work before releasing
    # the window and its scene objects.
    def closeEvent(self, event):
        if getattr(self, "fpga_window", None) and self.fpga_window.busy:
            self.status.setText("Wait for the FPGA operation to finish before closing the viewer.")
            event.ignore()
            return
        if not self.confirm_edits():
            event.ignore()
            return
        self.scope.shutdown()
        self._cancel_digital_preview()
        self.digital.shutdown()
        event.accept()
