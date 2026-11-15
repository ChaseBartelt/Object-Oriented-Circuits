"""Quantity propagation and symbolic RC/RL response kernels."""

import cmath
import math
from numbers import Number

import sympy as sp

from circuitlab.model.graph import CircuitError
from circuitlab.math.numbers import close, phasor, principal
from circuitlab.math.quantities import QUANTITIES

_INITIAL, _STEADY, _T, _TAU = sp.symbols("initial steady t tau", real=True)
STEP_RESPONSE = _STEADY + (_INITIAL - _STEADY) * sp.exp(-_T / _TAU)
_step_tail = sp.lambdify((_INITIAL, _STEADY, _T, _TAU), STEP_RESPONSE, "math")

COMPLEX = {"V", "I", "Z", "ZL", "ZC"}
POSITIVE = {"C", "L", "tauRL", "f", "T", "w"}
NONNEGATIVE = set(
    "tauRC t Vmag Imag Zmag Vpk Ipk Vpp Ipp Vcmag Ilmag Ec El Ein Eout Pin Pout S XL P0 speedRatio shaftW Pmech torque".split()
)


def solve_known(mode, known, model="generic"):
    """Return solved quantities, diagnostics, and the equation for each result."""
    return QuantitySolver(mode, known, model).solve()


class QuantitySolver:
    """Apply reversible circuit equations to the supplied quantities."""

    def __init__(self, mode, known, model):
        if (
            mode not in QUANTITIES
            or model not in {"generic", "rc", "rl"}
            or (mode != "transient" and model != "generic")
        ):
            raise CircuitError("A series step model requires transient analysis.")
        self.mode, self.model = mode, model
        self.known = known
        self.values = dict(known)
        self.equations = {}
        self.diagnostics = []
        for key, value in known.items():
            if key not in QUANTITIES[mode]:
                raise CircuitError(f"{key} is unavailable in {mode} analysis.")
            if problem := self.invalid(key, value):
                raise CircuitError(f"{key} {problem}.")

    def solve(self):
        rules = {"dc": self.dc, "ac": self.ac, "transient": self.transient}[self.mode]
        # Rules can fill in values that earlier rules needed on the next pass.
        while True:
            count = len(self.values)
            self.time_constants()
            rules()
            if len(self.values) == count:
                return self.values, self.diagnostics, self.equations

    def has(self, *keys):
        return all(key in self.values for key in keys)

    def zero(self, key):
        return self.values.get(key) == 0

    def note(self, message):
        if message not in self.diagnostics:
            self.diagnostics.append(message)

    def invalid(self, key, value):
        if not isinstance(value, Number) or not cmath.isfinite(value):
            return "must be a finite numeric scalar"
        if value.imag and (self.mode != "ac" or key not in COMPLEX):
            return "must be real"
        v = value.real
        if key in POSITIVE and v <= 0:
            return "must be positive"
        if key == "R" and self.model != "generic" and v <= 0:
            return "must be positive for a series step model"
        if key == "tauRC" and self.model == "rc" and v <= 0:
            return "must be positive for a series RC step model"
        if (key in NONNEGATIVE or (key == "R" and self.mode != "ac")) and v < 0:
            return "must be nonnegative"
        if key == "XC" and v >= 0:
            return "must be negative for a finite capacitive reactance"
        if key == "eta" and not 0 <= v <= 1:
            return "must be between 0 and 1"
        if key == "pf" and abs(v) > 1:
            return "must be between -1 and 1"
        return ""

    def offer(self, key, value, equation, scale=0, phase=None):
        """Record a derived value or diagnose a disagreement with an existing one."""
        if key == "pf" and abs(abs(value) - 1) <= 1e-9:
            value = math.copysign(1, value)
        if problem := self.invalid(key, value):
            self.note(f"Conflict: calculated {key} {problem}.")
        elif not self.has(key):
            self.values[key] = value.real if value.imag == 0 else value
            self.equations[key] = equation
        else:
            compare_phase = key.startswith("phase") if phase is None else phase
            same = (
                abs(principal(self.values[key] % 360 - value % 360)) <= 1e-7
                if compare_phase
                else close(self.values[key], value, scale)
            )
            if not same:
                self.note(f"Conflict: incompatible values for {key}.")

    def product(self, out, left, right="", factor=1, scale=0, label=None):
        """Apply a product equation in either direction when enough terms are known."""
        v, has, offer = self.values, self.has, self.offer
        if label is None:
            label = {1: "", 0.5: "½", math.sqrt(2): "√2", 2 * math.pi: "2π"}.get(
                factor, f"{factor:.8g}"
            )
        term = " x ".join(filter(None, (label, left, right)))
        equation = f"{out} = {term}"
        if not right:
            if factor == 0 or has(left):
                offer(out, factor * v.get(left, 0), equation, scale)
            elif has(out):
                offer(
                    left,
                    v[out] / factor,
                    f"{left} = {out}" + (f" / {label}" if label else ""),
                )
        elif self.zero(left) or self.zero(right):
            offer(out, 0, equation, scale)
        elif has(left, right):
            offer(out, factor * (v[left] * v[right]), equation, scale)
        else:
            for known, missing in ((left, right), (right, left)):
                if has(out, known):
                    term = " x ".join(filter(None, (label, known)))
                    offer(
                        missing,
                        v[out] / factor / v[known],
                        f"{missing} = {out} / ({term})",
                    )
                    break

    def square_product(self, out, linear, root, factor=1, magnitude=True, scale=0):
        """Apply a product with one squared term, respecting sign ambiguity."""
        v, has, offer = self.values, self.has, self.offer
        label = "½ x " if factor == 0.5 else ""
        equation = f"{out} = {label}{linear} x {root}²"
        if self.zero(root) or self.zero(linear):
            offer(out, 0, equation, scale)
        elif has(linear, root):
            offer(out, factor * (v[linear] * v[root]) * v[root], equation, scale)
        elif has(out, root):
            offer(
                linear,
                v[out] / factor / v[root] / v[root],
                f"{linear} = {out} / ({label}{root}²)",
            )
        elif has(out, linear):
            square = v[out] / factor / v[linear]
            if square < 0:
                self.note(f"Conflict: {out} and {linear} imply a negative square.")
            elif magnitude or square == 0:
                offer(root, math.sqrt(square), f"{root} = √({out} / ({label}{linear}))")

    def square_of_product(self, root, left, right):
        v, has, offer = self.values, self.has, self.offer
        equation = f"{root} = √({left} x {right})"
        if self.zero(left) or self.zero(right):
            offer(root, 0, equation)
        elif has(left, right):
            square = v[left] * v[right]
            if square < 0:
                self.note(f"Conflict: {left} and {right} imply a negative square.")
            else:
                offer(root, math.sqrt(square), equation)
        else:
            for known, missing in ((left, right), (right, left)):
                if has(root, known):
                    offer(
                        missing,
                        (v[root] / v[known]) * v[root],
                        f"{missing} = {root}² / {known}",
                    )
                    break

    def magnitude(self, signed, mag):
        if self.has(signed):
            self.offer(mag, abs(self.values[signed]), f"{mag} = |{signed}|")
        elif self.zero(mag):
            self.offer(signed, 0, f"{signed} = 0 when {mag} = 0")

    def time_constants(self):
        v = self.values
        if v.get("R", 0) < 0:
            return
        if "C" in v or "tauRC" in v:
            self.product("tauRC", "R", "C")
        if "L" in v or "tauRL" in v:
            if not self.zero("R"):
                self.product("L", "tauRL", "R")
            elif "L" in v:
                self.note("Time constant tauRL is undefined at R = 0.")

    def dc(self):
        v, has, product = self.values, self.has, self.product
        self.magnitude("V", "Vmag")
        self.magnitude("I", "Imag")
        product("P", "V", "I")
        if has("R") or (has("V", "I") and v["I"] and v["V"] / v["I"] >= 0):
            product("V", "I", "R")
        if has("R") or v.get("P", -1) >= 0:
            product("P", "Vmag", "Imag")
            if has("R") or (has("Vmag", "Imag") and v["Imag"]):
                product("Vmag", "Imag", "R")
            self.square_product("P", "R", "Imag")
            if has("R") or (has("P", "Vmag") and v["P"]):
                self.square_of_product("Vmag", "P", "R")
            for signed, mag, partner in (("V", "Vmag", "I"), ("I", "Imag", "V")):
                if not has(signed) and has(mag, partner) and v[partner]:
                    self.offer(
                        signed,
                        math.copysign(v[mag], v[partner]),
                        f"{signed} = {mag} x sign({partner})",
                    )
        if set(v) & {"t", "E", "Q"}:
            product("E", "P", "t")
            product("Q", "I", "t")
            product("E", "V", "Q")
        if set(v) & {"C", "Ec", "Qc"}:
            product("Qc", "C", "V")
            self.square_product("Ec", "C", "Vmag", 0.5)
        if set(v) & {"L", "El"}:
            self.square_product("El", "L", "Imag", 0.5)
        product("Eout", "eta", "Ein")
        product("Pout", "eta", "Pin")
        if has("t"):
            product("Ein", "Pin", "t")
            product("Eout", "Pout", "t")

    def ac(self):
        self.waveform()
        self.impedance()
        self.ac_power()
        for out, a, b, trigger in (
            ("Pmech", "P", "eta", "eta"),
            ("shaftW", "w", "speedRatio", "speedRatio"),
            ("Pmech", "torque", "shaftW", "torque"),
        ):
            if out in self.values or trigger in self.values:
                self.product(out, a, b)

    def waveform(self):
        v, offer = self.values, self.offer
        self.product("w", "f", factor=2 * math.pi)
        for a, b in (("T", "f"), ("f", "T")):
            if v.get(b):
                offer(a, 1 / v[b], f"{a} = 1 / {b}")
                break
        if "dphi" in self.known or self.has("dt", "f"):
            self.product("dphi", "f", "dt", -360)
        for z in ("V", "I", "Z"):
            mag, phase = z + "mag", "phase" + z
            if z in v:
                offer(mag, abs(v[z]), f"{mag} = |{z}|")
                if v[z]:
                    offer(phase, math.degrees(cmath.phase(v[z])), f"{phase} = ∠{z}")
            elif self.zero(mag):
                offer(z, 0, f"{z} = 0 when {mag} = 0")
            elif self.has(mag, phase):
                offer(z, phasor(v[mag], v[phase]), f"{z} = {mag} ∠ {phase}")
        for key in "VI":
            self.product(key + "pk", key + "mag", factor=math.sqrt(2))
            self.product(key + "pp", key + "pk", factor=2)

    def impedance(self):
        v, has, offer = self.values, self.has, self.offer
        if has("R", "X"):
            offer("Z", complex(v["R"], v["X"]), "Z = R + jX")
        if has("Z"):
            for key, attr in (("R", "real"), ("X", "imag")):
                offer(key, getattr(v["Z"], attr), f"{key} = {attr}(Z)", abs(v["Z"]))
        if has("Z") or (has("V", "I") and v["I"]):
            self.product("V", "I", "Z")
        if has("Zmag") or (has("Vmag", "Imag") and v["Imag"]):
            self.product("Vmag", "Imag", "Zmag")
        if has("phaseV", "phaseI"):
            offer(
                "dphi",
                principal(v["phaseV"] - v["phaseI"]),
                "dphi = phaseV - phaseI",
                phase=True,
            )
        if has("phaseZ"):
            offer("dphi", v["phaseZ"], "dphi = phaseZ", phase=True)
        if has("phaseI", "dphi") and not self.zero("Vmag"):
            offer(
                "phaseV", principal(v["phaseI"] + v["dphi"]), "phaseV = phaseI + dphi"
            )
        if has("phaseV", "dphi") and not self.zero("Imag"):
            offer(
                "phaseI", principal(v["phaseV"] - v["dphi"]), "phaseI = phaseV - dphi"
            )
        if has("Zmag", "dphi") and v["Zmag"]:
            offer("phaseZ", principal(v["dphi"]), "phaseZ = dphi")
        self.product("XL", "w", "L")
        for out, a, b in (("XC", "w", "C"), ("C", "XC", "w"), ("w", "XC", "C")):
            if v.get(a) and v.get(b):
                offer(out, (-1 / v[a]) / v[b], f"{out} = -1 / ({a} x {b})")
                break
        for z, x in (("ZL", "XL"), ("ZC", "XC")):
            if has(z):
                if not close(v[z].real, 0, abs(v[z])):
                    self.note(f"Conflict: {z} must be purely reactive.")
                else:
                    offer(x, v[z].imag, f"{x} = imag({z})")
            elif has(x):
                offer(z, 1j * v[x], f"{z} = j x {x}")

    def ac_power(self):
        v, has, offer = self.values, self.has, self.offer
        self.product("S", "Vmag", "Imag")
        if self.zero("S"):
            offer("Qac", 0, "Qac = 0 when S = 0")
        scale = v.get("S", abs(v.get("V", 0)) * abs(v.get("I", 0)))
        if has("dphi") and (
            has("phaseV", "phaseI")
            or has("phaseZ")
            or has("V", "I")
            or set(self.known) & {"P", "S", "Qac", "pf"}
        ):
            rotation = phasor(1, v["dphi"])
            offer("pf", rotation.real, "pf = cos(dphi)", 1)
            self.product(
                "Qac", "S", factor=rotation.imag, scale=scale, label="sin(dphi)"
            )
        self.product("P", "S", "pf", scale=scale)
        self.square_product("P", "R", "Imag", scale=scale)
        self.square_product("Qac", "X", "Imag", scale=scale)
        if has("V", "I"):
            power = v["V"] * v["I"].conjugate()
            offer("P", power.real, "P = real(V x conj(I))", abs(power))
            offer("Qac", power.imag, "Qac = imag(V x conj(I))", abs(power))
        for missing, present in (("Qac", "P"), ("P", "Qac")):
            if not has(missing) and has("S", present) and v["S"] == abs(v[present]):
                offer(missing, 0, f"{missing} = 0 when S = |{present}|")
        if has("P", "Qac"):
            power = complex(v["P"], v["Qac"])
            offer("S", abs(power), "S = |P + jQac|")
            if power:
                offer(
                    "dphi",
                    math.degrees(cmath.phase(power)),
                    "dphi = ∠(P + jQac)",
                    phase=True,
                )
            if v.get("I"):
                offer("V", power / v["I"].conjugate(), "V = (P + jQac) / conj(I)")
            if v.get("V"):
                offer("I", (power / v["V"]).conjugate(), "I = conj((P + jQac) / V)")
        for key in ("P", "Qac"):
            if (
                has("S", key)
                and abs(v[key]) > v["S"]
                and not close(abs(v[key]), v["S"])
            ):
                self.note(f"Conflict: apparent power S is smaller than |{key}|.")

    def equal(self, a, b):
        for source, target in ((a, b), (b, a)):
            if self.has(source):
                self.offer(target, self.values[source], f"{target} = {source}")
                break

    def sum_of(self, out, a, b):
        v, has, offer = self.values, self.has, self.offer
        if has(a, b):
            offer(out, v[a] + v[b], f"{out} = {a} + {b}", max(abs(v[a]), abs(v[b])))
        elif has(out, a):
            offer(b, v[out] - v[a], f"{b} = {out} - {a}")
        elif has(out, b):
            offer(a, v[out] - v[b], f"{a} = {out} - {b}")

    def transient(self):
        v, has, offer, model = self.values, self.has, self.offer, self.model
        self.magnitude("Vc", "Vcmag")
        self.magnitude("Il", "Ilmag")
        if model == "rc" or set(v) & {"C", "Q", "Ec"}:
            self.product("Q", "C", "Vc")
            self.square_product("Ec", "C", "Vcmag", 0.5)
        if model == "rl" or set(v) & {"L", "El"}:
            self.square_product("El", "L", "Ilmag", 0.5)
        if model == "generic":
            for sample, initial, drive, part in (
                ("Vc", "V0", "Ic", "C"),
                ("Il", "I0", "Vl", "L"),
            ):
                if set(v) & {sample, initial, drive, part}:
                    self.ramp(sample, initial, drive, part)
        else:
            if model == "rc":
                self.equal("Vfinal", "Vsrc")
                offer("Ifinal", 0, "Ifinal = 0 (capacitor at steady state)")
                self.drop()
                self.equal("I", "Ic")
                self.sum_of("Vsrc", "V", "Vc")
                self.decay("Vc", "V0", "Vfinal", "tauRC")
            else:
                self.product("Vsrc", "Ifinal", "R")
                if self.zero("Vsrc"):
                    offer("Ifinal", 0, "Ifinal = Vsrc / R = 0")
                offer("Vfinal", 0, "Vfinal = 0 (inductor at steady state)")
                self.drop()
                self.equal("I", "Il")
                self.sum_of("Vsrc", "V", "Vl")
                self.decay("Il", "I0", "Ifinal", "tauRL")
        if model != "generic" or has("R"):
            self.product("V", "I", "R")
            self.square_product("P", "R", "I", magnitude=False)
        if model != "generic":
            if self.zero("V"):
                offer("I", 0, "I = V / R = 0")
            self.square_product("P0", "R", "I0", magnitude=False)
        self.product("P", "V", "I")

    def drop(self):
        v, has, offer = self.values, self.has, self.offer
        if has("I0", "R"):
            drop = v["I0"] * v["R"]
            if has("V0"):
                offer(
                    "Vsrc",
                    v["V0"] + drop,
                    "Vsrc = V0 + I0 x R",
                    max(abs(drop), abs(v["V0"])),
                )
            elif has("Vsrc"):
                offer("V0", v["Vsrc"] - drop, "V0 = Vsrc - I0 x R")
        elif has("Vsrc", "V0"):
            diff = v["Vsrc"] - v["V0"]
            if diff == 0:
                offer("I0", 0, "I0 = 0 when Vsrc = V0")
            elif v.get("R"):
                offer("I0", diff / v["R"], "I0 = (Vsrc - V0) / R")
            elif v.get("I0"):
                offer("R", diff / v["I0"], "R = (Vsrc - V0) / I0")

    def ramp(self, sample, initial, drive, part):
        v, has, offer = self.values, self.has, self.offer
        if self.zero(drive) or self.zero("t"):
            self.equal(sample, initial)
        if has(drive, "t", part):
            delta = v[drive] * (v["t"] / v[part])
            if has(initial):
                offer(
                    sample,
                    v[initial] + delta,
                    f"{sample} = {initial} + {drive} x t / {part}",
                    abs(v[initial]) + abs(delta),
                )
            elif has(sample):
                offer(
                    initial,
                    v[sample] - delta,
                    f"{initial} = {sample} - {drive} x t / {part}",
                )
        if has(sample, initial):
            diff = v[sample] - v[initial]
            if v.get("t") and diff == 0:
                offer(drive, 0, f"{drive} = 0 when {sample} = {initial} and t > 0")
            elif v.get("t") and has(part):
                offer(
                    drive,
                    diff * (v[part] / v["t"]),
                    f"{drive} = {part} x ({sample} - {initial}) / t",
                )
            elif v.get(drive) and has(part):
                offer(
                    "t",
                    diff * (v[part] / v[drive]),
                    f"t = {part} x ({sample} - {initial}) / {drive}",
                )
            elif has(drive, "t") and diff:
                offer(
                    part,
                    v[drive] * (v["t"] / diff),
                    f"{part} = {drive} x t / ({sample} - {initial})",
                )

    def decay(self, sample, initial, steady, tau):
        v, has, offer = self.values, self.has, self.offer
        if has(initial, steady) and v[initial] == v[steady]:
            offer(sample, v[initial], f"{sample} = {initial} = {steady}")
        if self.zero("t"):
            self.equal(sample, initial)
        if has("t", tau):
            exponent = -v["t"] / v[tau]
            a, complement = math.exp(exponent), -math.expm1(exponent)
            if has(initial, steady):
                answer = (
                    v[initial] + (v[steady] - v[initial]) * complement
                    if a > 0.5
                    else _step_tail(v[initial], v[steady], v["t"], v[tau])
                )
                offer(
                    sample,
                    answer,
                    f"{sample} = {steady} + ({initial} - {steady}) x exp(-t/{tau})",
                    max(abs(v[initial]), abs(v[steady])),
                )
            elif has(sample, steady) and a:
                offer(
                    initial,
                    v[steady] + (v[sample] - v[steady]) / a,
                    f"{initial} = {steady} + ({sample} - {steady}) x exp(t/{tau})",
                )
            elif has(sample, initial) and complement:
                offer(
                    steady,
                    v[initial] + (v[sample] - v[initial]) / complement,
                    f"{steady} = {initial} + ({sample} - {initial}) / (1 - exp(-t/{tau}))",
                )
        elif has(sample, initial, steady) and v[initial] != v[steady]:
            fraction = (v[sample] - v[steady]) / (v[initial] - v[steady])
            if not 0 < fraction <= 1:
                self.note(f"Conflict: {sample} is outside the finite step response.")
            elif fraction == 1:
                offer("t", 0, f"t = 0 when {sample} = {initial}")
            elif has(tau):
                offer(
                    "t",
                    -v[tau] * math.log(fraction),
                    f"t = -{tau} x ln(({sample} - {steady}) / ({initial} - {steady}))",
                )
            elif has("t"):
                offer(
                    tau,
                    -v["t"] / math.log(fraction),
                    f"{tau} = -t / ln(({sample} - {steady}) / ({initial} - {steady}))",
                )
