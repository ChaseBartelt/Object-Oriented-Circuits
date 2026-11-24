"""Rich circuit highlighting uses the actual VS Code TextMate tokenizer."""

import json
import os
import subprocess
import unittest

from tests.tooling.test_ide_grammar import GRAMMAR, TOKENIZE, vscode_engine


class RichTextMateColorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        engine = vscode_engine()
        if engine is None:
            raise unittest.SkipTest("VS Code's bundled TextMate/Oniguruma is unavailable.")
        executable, modules, wasm = engine
        cls.sources = {
            "perfboard": [
                "perf[A-R,1-24] exclude[Vwavegen, Negative, Positive];",
                'include("perf[A-R,1-24] exclude[V1].ckt");',
                "// perf[A-R,1-24] exclude[V1];",
            ],
            "behavior": [
                "analog {",
                "    property offset: V = 2mV;",
                "    properties {gain=10; output.max=12V; enabled=true;}",
                "    local=integrate(V(input),time);",
                "    V(out)=min(gain*local+offset,output.max);",
                "}",
                "digital {out=(a & b) | (c ^ d);}",
                "analog:math V(other)=clamp(V(input),0V,5V);",
            ],
            "models": [
                "analog (op1,op2) {",
                "    common.min=vminus+3V;",
                "    delay.rise=115ns;",
                "    output.type=open_collector;",
                "    gain=ideal;",
                "}",
                "analog oocCollectorComparator {response.time=1.3us;}",
                "oocCollectorComparator(vplus,vminus,a,b,out,vminus);",
            ],
            "calls": [
                "R1(1k) between(input,w*i); C1(10nF) between(w^i,0);",
                "op1(w^,0,w*i);",
                "transQ1(base,w^i,0,npn);",
                "Follow(w$i,wi*); Follow(wi^,out);",
                'include("chips/analog.ckt"); # analog digital integrate(1)',
                "clock(clk,50MHz) {every(1ms) {out=!out;}}",
            ],
            "lookalikes": [
                "analog=input; digital=output;",
                "analog_signal=wire; digital_signal=wire;",
                'include("digital properties offset.ckt");',
                "# analog properties {gain=1V;} digital integrate(time)",
                "out=sin(V(input))+square(phase,0.5);",
            ],
        }
        completed = subprocess.run(
            [str(executable), "-e", TOKENIZE, str(modules), str(wasm), str(GRAMMAR)],
            input=json.dumps({name:"\n".join(lines) for name,lines in cls.sources.items()}),
            capture_output=True, text=True, timeout=30,
            env={**os.environ,"ELECTRON_RUN_AS_NODE":"1"},
        )
        if completed.returncode:
            raise AssertionError(completed.stderr)
        cls.rows = json.loads(completed.stdout)

    def scopes(self, case, row, text):
        source = self.rows[case][row]
        start = source["line"].index(text)
        return [next(token["scopes"] for token in source["tokens"]
                     if token["startIndex"] <= column < token["endIndex"])
                for column in range(start,start+len(text))]

    def assert_scope(self, case, row, text, scope):
        for scopes in self.scopes(case,row,text):
            self.assertIn(scope,scopes,(case,row,text,scopes))

    def test_analog_digital_property_declarations_and_units_have_distinct_scopes(self):
        self.assert_scope("behavior",0,"analog","support.type.analog.ooc")
        self.assert_scope("behavior",6,"digital","keyword.control.digital.ooc")
        self.assert_scope("behavior",1,"property","storage.type.model-property.ooc")
        self.assert_scope("behavior",2,"properties","storage.type.model-properties.ooc")
        self.assert_scope("behavior",1,"offset","variable.other.property.ooc")
        self.assert_scope("behavior",1,"2","constant.numeric.ooc")
        self.assert_scope("behavior",1,"mV","constant.other.unit.ooc")
        self.assert_scope("behavior",2,"true","constant.language.boolean.ooc")
        self.assert_scope("behavior",7,"analog","support.type.analog.ooc")

    def test_perfboard_keywords_keep_comments_and_strings_protected(self):
        self.assert_scope("perfboard", 0, "perf", "storage.type.ooc")
        self.assert_scope("perfboard", 0, "exclude", "keyword.other.placement.ooc")
        self.assert_scope("perfboard", 1, "perf[A-R,1-24] exclude[V1].ckt", "string.quoted.double.ooc")
        self.assert_scope("perfboard", 2, "perf[A-R,1-24] exclude[V1]", "comment.line.double-slash.ooc")

    def test_dotted_model_fields_and_references_stay_whole_and_targets_are_components(self):
        for case,row,text in (("models",1,"common.min"),("models",2,"delay.rise"),
                              ("behavior",4,"output.max")):
            self.assert_scope(case,row,text,"variable.other.property.ooc")
        self.assert_scope("models",0,"op1","support.function.ooc")
        self.assert_scope("models",0,"op2","support.function.ooc")
        self.assert_scope("models",6,"oocCollectorComparator","support.function.ooc")
        self.assert_scope("models",7,"oocCollectorComparator","support.function.ooc")
        self.assert_scope("models",3,"open_collector","constant.language.output-type.ooc")
        self.assert_scope("models",4,"ideal","constant.language.model.ooc")

    def test_math_state_and_voltage_reads_keep_function_scopes(self):
        for case,row,text in (("behavior",3,"integrate"),("behavior",3,"V"),
                              ("behavior",4,"min"),("behavior",7,"clamp"),
                              ("lookalikes",4,"sin"),("lookalikes",4,"square")):
            self.assert_scope(case,row,text,"support.function.ooc")

    def test_connection_regions_preserve_builtin_calls_components_counters_and_control(self):
        self.assert_scope("calls",0,"C1","entity.name.function.component.ooc")
        self.assert_scope("calls",1,"op1","support.function.ooc")
        self.assert_scope("calls",2,"transQ1","support.function.ooc")
        self.assert_scope("calls",2,"npn","constant.language.polarity.ooc")
        self.assert_scope("calls",3,"Follow","entity.name.function.ooc")
        self.assertIn("variable.other.readwrite.ooc",self.scopes("calls",3,"w$i")[0])
        self.assert_scope("calls",3,"$","keyword.operator.ooc")
        for name, operator in (("wi*", "*"), ("wi^", "^")):
            scopes = self.scopes("calls", 3, name)
            self.assertIn("variable.other.readwrite.ooc", scopes[0])
            self.assertIn("keyword.operator.ooc", scopes[name.index(operator)])
        self.assert_scope("calls",5,"clock","keyword.control.ooc")
        self.assert_scope("calls",5,"every","keyword.control.ooc")

    def test_similar_signal_names_strings_and_comments_do_not_become_behavior_keywords(self):
        for row,text in ((0,"analog"),(0,"digital"),(1,"analog_signal"),(1,"digital_signal")):
            self.assert_scope("lookalikes",row,text,"variable.other.readwrite.ooc")
        self.assert_scope("lookalikes",2,"digital properties offset.ckt","string.quoted.double.ooc")
        self.assert_scope("lookalikes",3,"analog properties","comment.line.number-sign.ooc")
        self.assert_scope("calls",4,"chips/analog.ckt","string.quoted.double.ooc")


if __name__ == "__main__":
    unittest.main()
