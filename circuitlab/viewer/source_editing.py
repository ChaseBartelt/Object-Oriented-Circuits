"""Source-aware component tools shared by the IDE viewer and optional editor."""
# Value changes are transactions over an accepted source revision and the current geometry.
# Preview gestures stay local; commits use checked before/after text and wait for IDE
# acknowledgement before recording undo or treating the new source as authoritative.
from concurrent.futures import ThreadPoolExecutor
from cmath import phase
from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from math import degrees
from uuid import uuid4

from PySide6.QtCore import QTimer, QSignalBlocker, QEventLoop
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QInputDialog, QMessageBox

from circuitlab.layout.persistence import pack_layout, embed_placement, strip_placement, layout_payload
from circuitlab.tooling.source_edits import SourceEdit, edit_switch, edit_value, source_target
from circuitlab.model.graph import CircuitError
from circuitlab.math.analysis import analyze
from circuitlab.math.numbers import format_value
from circuitlab.viewer.widgets import button, write_bytes
from circuitlab.viewer.schematic import PlacementUndo


@dataclass
class SourceUndo:
    edit: SourceEdit
    snapshot: dict


class SourceTools:
    def __init__(self, window, canvas):
        self.window = window
        self.view = window.schematic
        self.sender = None
        self.pending = None
        self.value_changes = {}
        self.history = self.view.undo_history
        self.internal_refresh = False
        self.identity = None
        self.position_edit = None
        self.position_generation = 0
        self.position_future = None
        self.position_submitted = -1
        self.position_reopen = None
        self.position_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix='potentiometer')
        window.destroyed.connect(lambda: self.position_worker.shutdown(wait=False, cancel_futures=True))
        self.position_timer = QTimer(window)
        self.position_timer.setInterval(80)
        self.position_timer.timeout.connect(self.preview_position)
        self.view.undo_handler = self.undo
        # Include the editing toolbar, which often keeps focus after a dialog.
        # The legacy text editor is outside this page and keeps its own undo.
        self.view.undo_shortcut.setParent(canvas.parentWidget() if hasattr(window, 'editor') else window)
        self.view.history_cleared.connect(self.source_changed)
        self.timeout = QTimer(window)
        self.timeout.setSingleShot(True)
        self.timeout.timeout.connect(lambda: self.result({'id':self.request_id, 'ok':False, 'error':
            'VS Code did not acknowledge the edit. Update the OOC extension and reopen the viewer; check your code before retrying.'}))
        self.view.source_editing = True
        self.view.value_requested.connect(self.change_value)
        self.view.potentiometer_requested.connect(self.open_potentiometer)
        self.view.switch_requested.connect(self.toggle_switch)
        self.view.position_changed.connect(self.adjust_position)
        self.view.optimization_finished.connect(lambda message: window.status.setText(message))
        row = QHBoxLayout()
        row.addStretch()
        optimize = button('Optimize wiring', self.view.optimize_wiring, row)
        optimize.setToolTip('Shorten routes and reposition parts while preserving connections. Ctrl+Z undoes it.')
        undo = button('Undo', self.undo, row)
        undo.setToolTip('Undo the last component edit or layout move (Ctrl+Z).')
        canvas.addLayout(row)

    # Resolve the active circuit, root document, current text, and include reader together.
    # Every proposed edit must refer to this same accepted document revision.
    def context(self):
        window = self.window
        if not window.path:
            raise ValueError('Save this circuit before editing components in the schematic.')
        if self.view.circuit is None:
            raise ValueError('Render the current source before editing components.')
        source = window.editor.toPlainText() if hasattr(window, 'editor') else window.session.source
        return self.view.circuit, window.path, source, window.read_linked_source

    def error(self, error):
        QMessageBox.warning(self.window, 'Edit circuit', str(error))

    def change_value(self, name):
        self.cancel_position()
        try:
            circuit, root, source, reader = self.context()
            _, _, _, match = source_target(circuit, name, root, source, reader)
            if 'value' not in match.groupdict():
                raise ValueError('This value is generated. Edit its parameter or definition in the IDE.')
            part = next(part for part in circuit.components if part.name == name)
            hint = (' in ohms (e.g. 2-j2 or 4+j4)' if part.kind == 'Z' else
                    ' as resistance, position (e.g. 10k,25%; position defaults to 50% from low)' if part.kind == 'P' else '')
            previous = (match['value'] or '').strip()
            value, ok = QInputDialog.getText(self.window, 'Edit component value',
                f'{name} value{hint} (blank = unspecified):\nShared definitions update all their instances.', text=previous)
            if ok and value.strip() != previous:
                self.apply(edit_value(circuit, name, value, root, source, reader))
        except (ValueError, OSError, CircuitError, StopIteration) as error:
            self.error(error)

    # Capture a checked source/value baseline for the gesture before displaying the slider.
    def open_potentiometer(self, name):
        if self.pending:
            return
        self.cancel_position()
        try:
            circuit, root, source, reader = self.context()
            path, before, _, match = source_target(circuit, name, root, source, reader)
            if 'value' not in match.groupdict():
                raise ValueError('This position is generated. Edit its parameter or definition in the IDE.')
            if hasattr(self.window, 'editor') and path != root:
                raise ValueError('Open this included definition in the editor to change it.')
            part = next(part for part in circuit.components if part.name == name and part.kind == 'P')
            # Shared definitions affect all instances on the same source statement.
            raw = match['name'].casefold()
            names = {part.name for part in circuit.components if part.kind == 'P'
                     and circuit.component_lines.get(part.name) == circuit.component_lines.get(name)
                     and circuit.component_files.get(part.name, root) == circuit.component_files.get(name, root)
                     and (part.name.casefold() == raw or part.name.casefold().startswith(raw + '_'))}
            self.position_edit = dict(name=name, circuit=circuit, solution=self.window.solution,
                                      context=(circuit, root, source, reader), path=path, before=before,
                                      resistance=(match['value'] or '').split(',', 1)[0].strip(), names=names,
                                      initial=round(part.position * 100), percent=round(part.position * 100),
                                      identity=self.current_identity(), drawn=None)
            self.view.show_position_control(name, part.position)
        except (ValueError, OSError, CircuitError, StopIteration) as error:
            self.error(error)

    def toggle_switch(self, name):
        """Commit one switch click through the normal checked source/undo path."""
        if self.pending:
            return
        self.cancel_position()
        try:
            circuit, root, source, reader = self.context()
            part = next(part for part in circuit.components if part.name == name and part.kind == 'K')
            self.apply(edit_switch(circuit, name, not part.closed, root, source, reader))
        except (ValueError, OSError, CircuitError, StopIteration) as error:
            self.error(error)

    def set_preview_solution(self, result):
        if hasattr(self.window, 'editor'):
            self.window.set_solution(result)
        else:
            self.window.session.solution = result
            self.window.update_results()

    def preview_scope(self, circuit):
        if self.window.scope.circuit is not circuit:
            self.window.scope.set_circuit(circuit, preserve_state=True)

    def cancel_position(self, *, restore=True):
        self.position_generation += 1
        self.position_timer.stop()
        edit, self.position_edit = self.position_edit, None
        if restore and edit and edit['drawn'] is not None:
            self.set_preview_solution(edit['solution'])
            if self.view.circuit is not None:
                self.view.preview_potentiometers(edit['circuit'])
            self.preview_scope(edit['circuit'])
        self.view.hide_position_control()

    # Coalesce preview updates during movement, then commit one final source edit for the
    # gesture so Undo restores the position before the user started dragging.
    def adjust_position(self, name, percent, final=False):
        edit = self.position_edit
        if not edit or edit['name'] != name or self.pending:
            return
        if self.current_identity() != edit['identity']:
            self.cancel_position(restore=False)
            return
        percent = max(0, min(100, int(percent)))
        if percent != edit['percent']:
            edit['percent'] = percent
            self.position_generation += 1
        if not final:
            if not self.position_timer.isActive():
                self.position_timer.start()
            return
        self.cancel_position()
        if percent == edit['initial']:
            self.open_potentiometer(name)
            return
        circuit, root, source, reader = edit['context']
        try:
            current = source if edit['path'] == root else reader(edit['path'])
            if current != edit['before']:
                raise ValueError('The source changed. Reload before adjusting this potentiometer.')
            plan = edit_value(circuit, name, f"{edit['resistance']},{percent}%", root, source, reader)
            self.position_reopen = name
            self.apply(plan)
            if not self.pending:
                self.position_reopen = None
        except (ValueError, OSError, CircuitError) as error:
            self.position_reopen = None
            self.error(error)

    # Compute previews independently from writes, and discard stale worker results when a
    # newer slider position or different document has superseded the request.
    def preview_position(self):
        edit = self.position_edit
        if not edit or self.current_identity() != edit['identity']:
            self.cancel_position(restore=False)
            return
        generation = self.position_generation
        if edit['drawn'] != generation:
            position = edit['percent'] / 100
            circuit = edit['circuit']
            if edit['percent'] != edit['initial']:
                circuit = replace(circuit, components=[replace(part, position=position) if part.name in edit['names'] else part
                                                       for part in circuit.components])
            edit['preview'] = circuit
            edit['drawn'] = generation
            self.view.preview_potentiometers(circuit)
            self.preview_scope(circuit)
        if self.position_future is not None:
            owner, future = self.position_future
            if not future.done():
                return
            self.position_future = None
            try:
                result = future.result()
            except (CircuitError, ValueError) as error:
                if owner == generation:
                    self.set_preview_solution(None)
                    self.view.position_reading.setText(str(error))
                    self.window.status.setText(str(error))
            else:
                if owner == generation:
                    self.set_preview_solution(result)
                    part = next(part for part in edit['preview'].components if part.name == edit['name'])
                    network = result.network
                    if network is not None and part.wiper in network.nodes:
                        voltage = network.voltage[network.nodes.index(part.wiper)]
                        reading = ('Wiper: ' + format_value(voltage, 'V') if result.circuit.mode == 'dc' else
                                   f"Wiper: {format_value(abs(voltage), 'V')} RMS, phase {degrees(phase(voltage)):.3g}°")
                        self.view.position_reading.setText(reading)
                        self.window.status.setText(f"{part.name} · {edit['percent']}% · {reading} (preview)")
        if self.position_future is None and self.position_submitted != generation:
            self.position_submitted = generation
            self.position_future = generation, self.position_worker.submit(analyze, edit['preview'])
        elif self.position_future is None:
            self.position_timer.stop()

    # Ignore presentation-only layout comments when comparing executable source revisions.
    def current_identity(self):
        window = self.window
        source = window.editor.toPlainText() if hasattr(window, 'editor') else window.session.source
        try:
            source = strip_placement(source)
        except ValueError:
            pass  # Malformed metadata is still a source revision, not a UI crash.
        return window.path, source.replace('\r\n', '\n')

    def source_changed(self):
        if self.internal_refresh:
            return
        self.cancel_position(restore=False)
        identity = self.current_identity()
        if identity != self.identity:
            self.history.clear()
        self.identity = identity

    @staticmethod
    def code(source):
        return strip_placement(source).replace('\r\n', '\n')

    def checkpoint(self):
        """An explicit Save accepts the live value edits made in the viewer."""
        self.value_changes.clear()

    def write_and_wait(self, plan):
        """Use the same checked write for Save and Discard, waiting for IDE acknowledgment."""
        outcome = []
        wait = QEventLoop(self.window)

        def saved(ok):
            outcome.append(ok)
            wait.quit()

        try:
            self.apply(plan, on_saved=saved)
            if self.pending:
                wait.exec()
        finally:
            wait.deleteLater()
        return bool(outcome and outcome[0])

    # Check all affected files before restoring values. Outside edits must not be overwritten
    # merely because the viewer still has an older before/after record.
    def discard_values(self):
        if self.pending:
            return False
        restored = set()
        ok = False
        try:
            plans = []
            # Check all files before restoring any, including edited chip definitions.
            for path, change in self.value_changes.items():
                current = path.read_bytes().decode('utf-8-sig')
                if self.code(current) == self.code(change.before):
                    restored.add(path)
                    continue
                if self.code(current) != self.code(change.after):
                    raise ValueError(f'{path.name} changed outside the viewer. Discard was cancelled to protect those edits.')
                plans.append(SourceEdit(path, change.after, change.before, 'Discard value changes'))
            for plan in plans:
                if not self.write_and_wait(plan):
                    break
                restored.add(plan.path)
                self.value_changes.pop(plan.path, None)
            else:
                self.checkpoint()
                self.history.clear()
                ok = True
        except (OSError, ValueError) as error:
            self.error(error)
        if restored:
            # A rejected IDE write or failed file switch can leave this window
            # open. Keep its source and values in sync with files already restored.
            remaining = [entry for entry in self.history
                         if not isinstance(entry, SourceUndo) or entry.edit.path not in restored]
            self.history.clear()
            self.history.extend(remaining)
            self.internal_refresh = True
            try:
                self.refresh(self.snapshot())
                self.identity = self.current_identity()
                self.window.status.setText('Discarded value changes.' if ok else
                    'Some values were restored; remaining changes are still unsaved.')
            except (OSError, ValueError) as error:
                self.error(error)
                ok = False
            finally:
                self.internal_refresh = False
        return ok

    def snapshot(self):
        records = {key: pack_layout(layout) for key, layout in self.view.level_layouts.items()}
        records.update(self.view.placements())
        return deepcopy(dict(records=records, expanded=set(self.view.expanded),
                    mode=self.view.layout_mode, offsets=dict(self.view.level_offsets),
                    automatic=self.view.level_layouts,
                    manual=self.view.manual_layouts,
                    rotations=self.view.rotation_bases,
                    logic=self.window.logic_placements() if hasattr(self.window, 'logic_placements')
                    else {'schematic': self.view.logic_placements()}))

    def undo(self):
        self.cancel_position()
        if self.pending or any(diagram.press is not None for diagram in self.view.behavior_diagrams.values()):
            return
        digital = getattr(self.window, 'digital', None)
        if digital is not None and digital.port_view.hasFocus():
            digital.port_view.undo_placement()
            return
        if self.pending or self.view.drag is not None or not self.history:
            return
        entry = self.history[-1]
        if isinstance(entry, PlacementUndo):
            self.history.pop()
            with QSignalBlocker(self.window.wire_style):
                self.window.wire_style.setCurrentIndex(self.window.wire_style.findData(entry.mode))
            self.view.restore_placement(entry)
            return
        edit = entry.edit
        inverse = SourceEdit(edit.path, edit.after, edit.before, 'Undo ' + edit.description)
        try:
            self.apply(inverse, undo_entry=entry)
        except (ValueError, OSError, CircuitError) as error:
            self.error(error)

    # Stage the source transaction and preserve geometry/history. When attached to the IDE,
    # wait for acknowledgement rather than assuming a requested write has already succeeded.
    def apply(self, plan, *, undo_entry=None, on_saved=None):
        if self.pending:
            raise ValueError('Wait for the previous source edit to finish.')
        window = self.window
        previous = self.value_changes.get(plan.path)
        if not on_saved and previous and self.code(plan.before) != self.code(previous.after):
            raise ValueError('The code changed outside the viewer. Save or resolve those edits before changing another value.')
        current = (window.editor.toPlainText() if hasattr(window, 'editor')
                   else plan.path.read_bytes().decode('utf-8-sig'))
        if current != plan.before and self.code(current) == self.code(plan.before):
            # Saving geometry must not invalidate an earlier value undo or let
            # that undo replace newer saved geometry with its old comment block.
            after = strip_placement(plan.after)
            if payload := layout_payload(current):
                records = {(level, mode): record for level, mode, record in payload['layouts']}
                after = embed_placement(after, payload['mode'], records,
                                        pin_overrides=payload.get('pin_overrides'),
                                        logic_layouts=payload.get('logic'))
            plan = SourceEdit(plan.path, current, after, plan.description)
        snapshot = None if on_saved else (undo_entry.snapshot if undo_entry else self.snapshot())
        self.operation = plan, snapshot, undo_entry, on_saved
        if hasattr(window, 'editor'):
            if plan.path != window.path:
                raise ValueError('Open this included definition in the editor to change it.')
            if window.editor.toPlainText() != plan.before:
                raise ValueError('The source changed. Render again before editing.')
            self.internal_refresh = True
            try:
                cursor = window.editor.textCursor()
                cursor.beginEditBlock()
                cursor.select(QTextCursor.SelectionType.Document)
                cursor.insertText(plan.after)
                cursor.endEditBlock()
                self.complete()
            finally:
                self.internal_refresh = False
        elif self.sender:
            self.pending = plan
            self.request_id = uuid4().hex
            self.view.setEnabled(False)
            payload = asdict(plan)
            payload['path'] = str(plan.path)
            self.sender({'type':'ooc-source-edit', 'id':self.request_id, **payload})
            self.timeout.start(15000)
            window.status.setText('Applying source edit in VS Code…')
        else:
            data = plan.path.read_bytes()
            if data.decode('utf-8-sig').replace('\r\n', '\n') != plan.before.replace('\r\n', '\n'):
                raise ValueError('The source changed on disk. Reload before editing.')
            text = plan.after.replace('\r\n', '\n')
            if b'\r\n' in data:
                text = text.replace('\n', '\r\n')
            encoded = text.encode('utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8')
            if write_bytes(window, plan.path, encoded, 'Edit circuit source'):
                self.complete()
            elif on_saved:
                on_saved(False)

    # Match the response to the pending request so a late acknowledgement cannot complete
    # or undo a different edit that is now active.
    def result(self, result):
        if not self.pending or result.get('id') != self.request_id:
            return
        self.timeout.stop()
        self.pending = None
        self.view.setEnabled(True)
        if result.get('ok'):
            self.complete()
        else:
            self.position_reopen = None
            self.error(result.get('error', 'VS Code could not apply the source edit.'))
            if callback := self.operation[3]:
                callback(False)

    def complete(self):
        plan, snapshot, undo_entry, on_saved = self.operation
        if on_saved:
            on_saved(True)
            self.identity = self.current_identity()
            return
        self.internal_refresh = True
        try:
            self.refresh(snapshot, restoring=undo_entry is not None)
            if not hasattr(self.window, 'editor'):
                first = self.value_changes.get(plan.path, plan).before
                if self.code(first) == self.code(plan.after):
                    self.value_changes.pop(plan.path, None)
                else:
                    self.value_changes[plan.path] = SourceEdit(plan.path, first, plan.after, plan.description)
            if undo_entry:
                self.history.pop()
            else:
                self.history.append(SourceUndo(plan, snapshot))
            self.identity = self.current_identity()
        finally:
            self.internal_refresh = False
        if name := self.position_reopen:
            self.position_reopen = None
            self.open_potentiometer(name)

    # Rebuild from the accepted source while restoring compatible presentation and scope state.
    def refresh(self, snapshot, *, restoring=False):
        window = self.window
        records, mode = snapshot['records'], snapshot['mode']
        if not hasattr(window, 'editor'):
            pins = dict(window.session.pin_overrides)
            window.session.load(window.path)
            window.session.pin_overrides = pins
        self.view.clear()
        logic = snapshot.get('logic', {})
        self.view.load_logic_placements(logic.get('schematic', {}))
        if hasattr(window, 'digital'):
            window.digital.load_logic_placements(logic.get('simulation', {}))
        with QSignalBlocker(window.wire_style):
            window.wire_style.setCurrentIndex(window.wire_style.findData(mode))
        self.view.layout_mode = mode
        self.view.placement_records = records
        window.compile_source(preserve_scope=True)
        if restoring:
            self.view.level_layouts = snapshot['automatic'].copy()
            self.view.manual_layouts = snapshot['manual'].copy()
            self.view.rotation_bases = snapshot['rotations'].copy()
        self.view.expanded.update(snapshot['expanded'])
        self.view.level_offsets.update(snapshot['offsets'])
        if self.view.circuit and (restoring or snapshot['expanded']):
            self.view._redraw()
        window.placement_changed()
        window.status.setText('Value updated in the source and schematic. Save keeps it; Discard restores values changed since the last Save.')
