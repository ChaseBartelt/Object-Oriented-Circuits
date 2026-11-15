# Write OOC in Visual Studio

Open and edit `.ckt` files in Visual Studio on Windows. The OOC TextMate grammar colors the source. **OOC Check** validates a saved file and **OOC Solve** runs the existing Python solver with results in Visual Studio's Output window.

## Set up the editor

1. Set up the project's `.venv` from the [Quick start](../../README.md#quick-start). You can reuse the environment that runs the OOC desktop app.
2. From the OOC repository, run this in PowerShell.

   ~~~powershell
   .\ide\visual-studio\Install-OocEditor.ps1
   ~~~

   To install the grammar by hand, copy [OOC.json](Syntaxes/OOC.json) into `%USERPROFILE%\.vs\Extensions\OOC\Syntaxes\`, creating the folders if necessary. Close and reopen any `.ckt` files after installation. [Microsoft's Visual Studio grammar guide](https://learn.microsoft.com/en-us/visualstudio/ide/adding-visual-studio-editor-support-for-other-languages?view=visualstudio)

3. In Visual Studio, open **Tools → External Tools**, click **Add**, and enter the settings printed by the script. Fill in one entry per command you want.

   | Field | Solve a circuit | Check a file |
   | --- | --- | --- |
   | Title | `OOC Solve` | `OOC Check` |
   | Command | Full path to the OOC repo's `.venv\Scripts\python.exe` | The same Python executable |
   | Arguments | `-m circuitlab.cli solve "$(ItemPath)"` | `-m circuitlab.cli check "$(ItemPath)"` |
   | Initial directory | Full path to the OOC repository | The same repository path |
   | Use Output window | Checked | Checked |

   Visual Studio substitutes the active file's full path for `$(ItemPath)`. Keep the quotes around it so circuit paths can contain spaces. [Microsoft's External Tools guide](https://learn.microsoft.com/en-us/visualstudio/ide/managing-external-tools?view=visualstudio) · [Output window](https://learn.microsoft.com/en-us/visualstudio/ide/output-window?view=visualstudio)

## Edit and run a circuit

Open [voltage_divider.ckt](../../examples/voltage_divider.ckt) directly in Visual Studio. Save the active circuit and its included files, then select **Tools → OOC Solve**. The divider reports 4 V at `out`. Use **Tools → OOC Check** to validate circuit structure or a standalone digital `chip`. For linked circuits, open and check the entry file that includes the other files.

The commands read saved files and resolve relative includes from the file that declares them. The Output window shows text results and compiler messages. Use the OOC desktop app for interactive schematics and waveforms.
