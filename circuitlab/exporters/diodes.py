"""Two-terminal SPICE subcircuits matching the native generic diode laws."""

import math
from circuitlab.model.diode import THERMAL_VOLTAGE


def diode_subcircuit(name, model):
    def number(value):
        return f"{value:.16g}"

    junction = "j" if model.resistance else "a"
    voltage = f"V({junction},k)"
    scale = number(THERMAL_VOLTAGE * model.ideality)
    def exponential(expression):
        return f"(exp(min(({expression}),40))*(1+max(({expression})-40,0)))"

    current = f"{exponential(f'{number(math.log(model.saturation))}+{voltage}/{scale}')}-{number(model.saturation)}"
    current += f"-{exponential(f'{number(math.log(model.breakdown_current))}+(-{voltage}-{number(model.breakdown)})/{scale}')}"
    current += f"-{number(model.photocurrent)}"
    if model.kind == "tunnel":
        x = f"({voltage}/{number(model.peak_voltage)})"
        current += f"+if({voltage}<=0,{number(math.e*model.peak_current/model.peak_voltage)}*{voltage},{number(model.peak_current)}*{x}*exp(min(1-{x},40)))"
    lines = [f".subckt {name} a k"]
    if model.resistance:
        lines.append(f"Rseries a j {number(model.resistance)}")
    lines.append(f"Bjunction {junction} k I={{{current}}}")
    if model.capacitance:
        exponent = number(1 - model.grading)
        potential, capacitance = number(model.potential), number(model.capacitance)
        reverse = f"{capacitance}*{potential}/{exponent}*(1-(1-min({voltage},0)/{potential})**{exponent})"
        charge = f"if({voltage}>=0,{capacitance}*{voltage},{reverse})"
        lines.append(f"Bcharge {junction} k I={{ddt({charge})}}")
    return (*lines, f".ends {name}")
