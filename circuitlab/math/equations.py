"""Quantity propagation and symbolic RC/RL response kernels."""
# Quantity inference repeatedly applies reversible rules until no new values appear.
# Every proposed value is checked against unit-domain/sign constraints and existing givens;
# its explanatory equation is retained so the UI can show the derivation.

import cmath
import math
from numbers import Number

import sympy as sp

from circuitlab.model.graph import CircuitError
from circuitlab.math.numbers import close, phasor, principal
from circuitlab.math.quantities import QUANTITIES

# Build the first-order step formula symbolically once, then compile it
# to an ordinary math callable. Individual solves use numeric evaluation,
# not a fresh symbolic equation solve. Domain checks below enforce the
# valid time and time-constant ranges for the selected series model.
_INITIAL, _STEADY, _T, _TAU = sp.symbols("initial steady t tau", real=True)
STEP_RESPONSE = _STEADY + (_INITIAL - _STEADY) * sp.exp(-_T / _TAU)
_step_tail = sp.lambdify((_INITIAL, _STEADY, _T, _TAU), STEP_RESPONSE, "math")

# Only these AC quantities may have an imaginary component. Magnitudes,
# power components, phases, and component values remain real scalars.
COMPLEX = {"V", "I", "Z", "ZL", "ZC"}
# Strictly positive quantities exclude zero before an inverse rule can
# divide by them. tauRC permits zero generically, but not in an RC step model.
POSITIVE = {"C", "L", "tauRL", "f", "T", "w"}
# Stored energies, magnitudes, elapsed time, and selected output measures
# can be zero. Signed voltage/current, real power P, reactive power Qac,
# and phase/delay values deliberately do not all share this restriction.
NONNEGATIVE = set(
    "tauRC t Vmag Imag Zmag Vpk Ipk Vpp Ipp Vcmag Ilmag Ec El Ein Eout Pin Pout S XL P0 speedRatio shaftW Pmech torque".split()
)


# Return the combined given/derived value dictionary, diagnostic messages,
# and derivation text for newly inferred quantities. Inputs are already
# parsed numbers in the catalog units (including degrees for angles);
# unit parsing belongs to the source layer.
def solve_known(mode, known, model="generic"):
    """Return solved quantities, diagnostics, and the equation for each result."""
    return QuantitySolver(mode, known, model).solve()


class QuantitySolver:
    """Apply reversible circuit equations to the supplied quantities."""

    # generic applies quantity relationships without assuming a series step
    # circuit. rc/rl additionally select a first-order response topology and
    # are therefore available only in transient mode.
    def __init__(self, mode, known, model):
        if (
            mode not in QUANTITIES
            or model not in {"generic", "rc", "rl"}
            or (mode != "transient" and model != "generic")
        ):
            raise CircuitError("A series step model requires transient analysis.")
        self.mode, self.model = mode, model
        # Retain the original givens separately from the growing working copy.
        # Some rules distinguish an explicitly supplied quantity from one that
        # was inferred later. Propagation does not edit the caller's dictionary.
        self.known = known
        self.values = dict(known)
        self.equations = {}
        self.diagnostics = []
        # Invalid givens stop construction. Invalid derived proposals instead
        # become diagnostics through offer(), preserving the accepted values.
        for key, value in known.items():
            if key not in QUANTITIES[mode]:
                raise CircuitError(f"{key} is unavailable in {mode} analysis.")
            if problem := self.invalid(key, value):
                raise CircuitError(f"{key} {problem}.")

    # Repeat the rules to a fixed point: a value found late in one pass can unlock an earlier
    # rule on the next pass. Stop when a pass adds no further quantities.
    def solve(self):
        rules = {"dc": self.dc, "ac": self.ac, "transient": self.transient}[self.mode]
        # Rules can fill in values that earlier rules needed on the next pass.
        # The value set grows monotonically: rules add missing keys and compare
        # against existing keys, rather than replacing them. A pass with no new
        # keys therefore marks completion, even if it reported conflicts.
        while True:
            count = len(self.values)
            self.time_constants()
            rules()
            if len(self.values) == count:
                return self.values, self.diagnostics, self.equations

    # Presence and truth are different: a known zero still satisfies has().
    def has(self, *keys):
        return all(key in self.values for key in keys)

    # Only an explicitly stored zero matches. A missing value is not assumed
    # zero, which is essential for avoiding invented results or zero divisors.
    def zero(self, key):
        return self.values.get(key) == 0

    # Fixed-point passes may rediscover the same conflict; keep one message.
    def note(self, message):
        if message not in self.diagnostics:
            self.diagnostics.append(message)

    # Return an explanation of a domain violation, or an empty string.
    # AC phasors/impedances can be complex, but non-finite numbers never enter
    # the accepted set. Real-valued complex scalars are permitted.
    def invalid(self, key, value):
        if not isinstance(value, Number) or not cmath.isfinite(value):
            return "must be a finite numeric scalar"
        if value.imag and (self.mode != "ac" or key not in COMPLEX):
            return "must be real"
        v = value.real
        if key in POSITIVE and v <= 0:
            return "must be positive"
        # A finite series RC/RL step needs positive resistance. Generic DC allows
        # R=0, and AC can represent a theoretical negative real impedance.
        if key == "R" and self.model != "generic" and v <= 0:
            return "must be positive for a series step model"
        if key == "tauRC" and self.model == "rc" and v <= 0:
            return "must be positive for a series RC step model"
        if (key in NONNEGATIVE or (key == "R" and self.mode != "ac")) and v < 0:
            return "must be nonnegative"
        # Capacitive reactance uses its signed convention XC=-1/(w*C). Efficiency
        # is a fraction; power factor is signed cos(dphi), so negative pf is allowed.
        if key == "XC" and v >= 0:
            return "must be negative for a finite capacitive reactance"
        if key == "eta" and not 0 <= v <= 1:
            return "must be between 0 and 1"
        if key == "pf" and abs(v) > 1:
            return "must be between -1 and 1"
        return ""

    # A rule proposes a value; it never silently overwrites an incompatible given.
    # Phase comparisons wrap around 360 degrees instead of comparing raw angles.
    def offer(self, key, value, equation, scale=0, phase=None):
        """Record a derived value or diagnose a disagreement with an existing one."""
        # Clamp tiny floating-point overshoot at +/-1 before validating the
        # power-factor domain; do not turn roundoff into a physical conflict.
        if key == "pf" and abs(abs(value) - 1) <= 1e-9:
            value = math.copysign(1, value)
        if problem := self.invalid(key, value):
            self.note(f"Conflict: calculated {key} {problem}.")
        # Keep the first accepted value and the equation that produced it.
        # Strip an exactly zero imaginary part for simpler downstream formatting.
        elif not self.has(key):
            self.values[key] = value.real if value.imag == 0 else value
            self.equations[key] = equation
        else:
            # phaseV/phaseI/phaseZ compare modulo a full turn. The explicit phase
            # override lets callers apply that rule to dphi as well. Other comparisons
            # use a numeric tolerance whose reference scale may be supplied by a rule.
            compare_phase = key.startswith("phase") if phase is None else phase
            same = (
                abs(principal(self.values[key] % 360 - value % 360)) <= 1e-7
                if compare_phase
                else close(self.values[key], value, scale)
            )
            if not same:
                self.note(f"Conflict: incompatible values for {key}.")

    # Represent out=factor*left*right, or out=factor*left when right is absent.
    # With enough nonzero data, rearrange the same relation to infer a missing
    # factor. Every direction goes through offer() for validation/conflicts.
    def product(self, out, left, right="", factor=1, scale=0, label=None):
        """Apply a product equation in either direction when enough terms are known."""
        v, has, offer = self.values, self.has, self.offer
        # Labels affect explanatory text only. A caller can retain a meaningful
        # factor such as sin(dphi) instead of displaying its evaluated decimal.
        if label is None:
            label = {1: "", 0.5: "½", math.sqrt(2): "√2", 2 * math.pi: "2π"}.get(
                factor, f"{factor:.8g}"
            )
        term = " x ".join(filter(None, (label, left, right)))
        equation = f"{out} = {term}"
        # In the one-operand form, a zero numeric factor determines out=0 even
        # when left is unknown. It never determines left by division through zero.
        if not right:
            if factor == 0 or has(left):
                offer(out, factor * v.get(left, 0), equation, scale)
            elif has(out):
                offer(
                    left,
                    v[out] / factor,
                    f"{left} = {out}" + (f" / {label}" if label else ""),
                )
        # A known zero factor fixes the product without knowing the other factor.
        # Handle this before inverses: 0=0*x does not identify x.
        elif self.zero(left) or self.zero(right):
            offer(out, 0, equation, scale)
        elif has(left, right):
            offer(out, factor * (v[left] * v[right]), equation, scale)
        else:
            # A known output and one nonzero factor determine the other. Known zero
            # factors were handled above, so an ambiguous 0/0 is never used here.
            for known, missing in ((left, right), (right, left)):
                if has(out, known):
                    term = " x ".join(filter(None, (label, known)))
                    offer(
                        missing,
                        v[out] / factor / v[known],
                        f"{missing} = {out} / ({term})",
                    )
                    break

    # Squaring can lose a sign, so infer a magnitude only where the quantity permits that
    # interpretation instead of choosing an arbitrary positive or negative solution.
    def square_product(self, out, linear, root, factor=1, magnitude=True, scale=0):
        """Apply a product with one squared term, respecting sign ambiguity."""
        # Here out=factor*linear*root**2. This covers I**2*R power and half-C/L
        # stored-energy formulas. magnitude decides whether a positive square
        # root is a valid inference for root, rather than a lost sign choice.
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
            # The forward equation loses the sign of root. A negative inferred square
            # is inconsistent; a positive square determines only a magnitude unless
            # the caller explicitly permits that interpretation. Zero has no ambiguity.
            square = v[out] / factor / v[linear]
            if square < 0:
                self.note(f"Conflict: {out} and {linear} imply a negative square.")
            elif magnitude or square == 0:
                offer(root, math.sqrt(square), f"{root} = √({out} / ({label}{linear}))")

    # Use root=sqrt(left*right) for a magnitude, such as |V|=sqrt(P*R).
    # Zero factors fix the result before any inverse division; a negative
    # radicand is diagnosed instead of producing a complex magnitude.
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

    # A signed value determines its absolute magnitude. The reverse direction
    # is unique only at zero; a positive magnitude alone cannot choose a sign.
    def magnitude(self, signed, mag):
        if self.has(signed):
            self.offer(mag, abs(self.values[signed]), f"{mag} = |{signed}|")
        elif self.zero(mag):
            self.offer(signed, 0, f"{signed} = 0 when {mag} = 0")

    # Apply tauRC=R*C and L=tauRL*R in all supported modes when relevant
    # quantities exist. Reversing these products can infer R, C, or L too.
    def time_constants(self):
        v = self.values
        # Negative R is allowed as a theoretical AC impedance, but does not
        # establish the passive time constants inferred by these rules.
        if v.get("R", 0) < 0:
            return
        if "C" in v or "tauRC" in v:
            self.product("tauRC", "R", "C")
        if "L" in v or "tauRL" in v:
            # At R=0, L/R has no finite RL time constant. Do not rearrange the product
            # into a fictitious L=0 or divide by zero; report the undefined case.
            if not self.zero("R"):
                self.product("L", "tauRL", "R")
            elif "L" in v:
                self.note("Time constant tauRL is undefined at R = 0.")

    # Combine signed Ohm/power relationships with magnitude-only resistor
    # relationships. Separate them so source power can remain negative and
    # a power/resistance pair does not invent voltage or current polarity.
    def dc(self):
        v, has, product = self.values, self.has, self.product
        self.magnitude("V", "Vmag")
        self.magnitude("I", "Imag")
        product("P", "V", "I")
        # Without a supplied resistance, infer a passive R only when the signed
        # V/I ratio is nonnegative. Opposite signs may describe delivered power;
        # they do not justify inventing a negative DC resistor.
        if has("R") or (has("V", "I") and v["I"] and v["V"] / v["I"] >= 0):
            product("V", "I", "R")
        # Use P=|V|*|I| only once resistance or nonnegative power establishes this
        # resistive magnitude context. Signed P=V*I above remains a separate rule.
        if has("R") or v.get("P", -1) >= 0:
            product("P", "Vmag", "Imag")
            if has("R") or (has("Vmag", "Imag") and v["Imag"]):
                product("Vmag", "Imag", "R")
            self.square_product("P", "R", "Imag")
            if has("R") or (has("P", "Vmag") and v["P"]):
                self.square_of_product("Vmag", "P", "R")
            # In the passive resistor context, a nonzero partner supplies the sign
            # that a magnitude lacks. With neither sign given, keep both unresolved.
            for signed, mag, partner in (("V", "Vmag", "I"), ("I", "Imag", "V")):
                if not has(signed) and has(mag, partner) and v[partner]:
                    self.offer(
                        signed,
                        math.copysign(v[mag], v[partner]),
                        f"{signed} = {mag} x sign({partner})",
                    )
        # Activate transfer/storage relationships when the corresponding quantities
        # are requested or supplied. Q is transferred charge; Qc is capacitor charge.
        # Energy transfer follows E=P*t=V*Q, while Q=I*t.
        if set(v) & {"t", "E", "Q"}:
            product("E", "P", "t")
            product("Q", "I", "t")
            product("E", "V", "Q")
        # Capacitor charge retains voltage polarity; stored energy uses |V|**2.
        # The inductor energy rule below likewise uses current magnitude squared.
        if set(v) & {"C", "Ec", "Qc"}:
            product("Qc", "C", "V")
            self.square_product("Ec", "C", "Vmag", 0.5)
        if set(v) & {"L", "El"}:
            self.square_product("El", "L", "Imag", 0.5)
        # Efficiency relates useful output to input. If elapsed time is known,
        # input/output energy and power can also unlock one another.
        product("Eout", "eta", "Ein")
        product("Pout", "eta", "Pin")
        if has("t"):
            product("Ein", "Pin", "t")
            product("Eout", "Pout", "t")

    # These groups feed one another across fixed-point passes: frequency and
    # phasors, impedance/phase relationships, and power. Optional motor rules
    # then connect electrical input to shaft speed, torque, and output power.
    def ac(self):
        self.waveform()
        self.impedance()
        self.ac_power()
        # Only activate each mechanical relationship when its output or trigger
        # quantity is present; ordinary AC data need not imply a motor model.
        for out, a, b, trigger in (
            ("Pmech", "P", "eta", "eta"),
            ("shaftW", "w", "speedRatio", "speedRatio"),
            ("Pmech", "torque", "shaftW", "torque"),
        ):
            if out in self.values or trigger in self.values:
                self.product(out, a, b)

    # V/I magnitudes are RMS quantities here. Angles are degrees; the peak
    # and peak-to-peak conversions below assume sinusoidal waveforms.
    def waveform(self):
        v, offer = self.values, self.offer
        # Relate frequency, period, and angular frequency reversibly. Positivity
        # validation prevents a zero period/frequency from entering reciprocals.
        self.product("w", "f", factor=2 * math.pi)
        for a, b in (("T", "f"), ("f", "T")):
            if v.get(b):
                offer(a, 1 / v[b], f"{a} = 1 / {b}")
                break
        # Use the delay relation when dphi was explicitly supplied, or when both
        # frequency and delay are known. A phasor angle inferred modulo 360 alone
        # does not establish a unique time delay. Positive delay gives negative phase.
        if "dphi" in self.known or self.has("dt", "f"):
            self.product("dphi", "f", "dt", -360)
        # Convert rectangular and polar descriptions in either direction.
        # A zero phasor fixes its magnitude but has no inferable phase angle.
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
        # For a sine wave: peak=sqrt(2)*RMS and peak-to-peak=2*peak. The product
        # helper can also reverse these conversions when a peak value is given.
        for key in "VI":
            self.product(key + "pk", key + "mag", factor=math.sqrt(2))
            self.product(key + "pp", key + "pk", factor=2)

    # Use signed reactance in Z=R+jX and phasor Ohm's law V=I*Z. Magnitude
    # and phase relations supplement the complex equation without selecting
    # an arbitrary absolute phase when only a phase difference is known.
    def impedance(self):
        v, has, offer = self.values, self.has, self.offer
        if has("R", "X"):
            offer("Z", complex(v["R"], v["X"]), "Z = R + jX")
        if has("Z"):
            for key, attr in (("R", "real"), ("X", "imag")):
                # Compare extracted R/X using |Z| as the reference scale, so a component
                # near zero is judged relative to the impedance it came from.
                offer(key, getattr(v["Z"], attr), f"{key} = {attr}(Z)", abs(v["Z"]))
        if has("Z") or (has("V", "I") and v["I"]):
            self.product("V", "I", "Z")
        if has("Zmag") or (has("Vmag", "Imag") and v["Imag"]):
            self.product("Vmag", "Imag", "Zmag")
        # dphi is voltage phase minus current phase; it is also the impedance
        # angle. principal() chooses the representative in [-180, 180), while
        # offer(..., phase=True) compares equivalent angles modulo a full turn.
        if has("phaseV", "phaseI"):
            offer(
                "dphi",
                principal(v["phaseV"] - v["phaseI"]),
                "dphi = phaseV - phaseI",
                phase=True,
            )
        if has("phaseZ"):
            offer("dphi", v["phaseZ"], "dphi = phaseZ", phase=True)
        # Do not infer a phasor angle when its magnitude is explicitly zero.
        # A missing magnitude is different: an angle may be known before magnitude.
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
        # Ideal reactances use XL=w*L and XC=-1/(w*C). XC remains negative rather
        # than becoming an unsigned magnitude; impedance is j times the reactance.
        self.product("XL", "w", "L")
        # Try each rearrangement of the capacitor relation. Sequential divisions
        # avoid first forming a potentially very large product in the denominator.
        for out, a, b in (("XC", "w", "C"), ("C", "XC", "w"), ("w", "XC", "C")):
            if v.get(a) and v.get(b):
                offer(out, (-1 / v[a]) / v[b], f"{out} = -1 / ({a} x {b})")
                break
        # An ideal individual inductor/capacitor impedance must be purely reactive.
        # Allow tiny real-part roundoff relative to |Z|, but diagnose a genuinely
        # resistive component instead of silently discarding it.
        for z, x in (("ZL", "XL"), ("ZC", "XC")):
            if has(z):
                if not close(v[z].real, 0, abs(v[z])):
                    self.note(f"Conflict: {z} must be purely reactive.")
                else:
                    offer(x, v[z].imag, f"{x} = imag({z})")
            elif has(x):
                offer(z, 1j * v[x], f"{z} = j x {x}")

    # RMS phasors give complex power V*conj(I)=P+jQac and apparent power
    # S=|V|*|I|. There is no extra 1/2 factor for RMS inputs. Qac is signed
    # reactive power, distinct from charge Q in DC/transient calculations.
    def ac_power(self):
        v, has, offer = self.values, self.has, self.offer
        self.product("S", "Vmag", "Imag")
        # Zero apparent power forces zero reactive power without assigning an
        # angle to a zero complex-power vector.
        if self.zero("S"):
            offer("Qac", 0, "Qac = 0 when S = 0")
        # Use apparent power as a comparison scale for small P/Q components,
        # especially near purely real or purely reactive operating points.
        scale = v.get("S", abs(v.get("V", 0)) * abs(v.get("I", 0)))
        # Gate the cosine/sine power rules on phasor, impedance-phase, or explicit
        # power context. A delay-derived dphi alone does not activate these rules.
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
        # Real/reactive power can also be related to RMS current squared times
        # R/X. A negative X produces negative reactive power for a capacitive load.
        self.product("P", "S", "pf", scale=scale)
        self.square_product("P", "R", "Imag", scale=scale)
        self.square_product("Qac", "X", "Imag", scale=scale)
        # Conjugating current makes the complex-power angle phaseV-phaseI.
        # The real and imaginary parts then determine P and Qac directly.
        if has("V", "I"):
            power = v["V"] * v["I"].conjugate()
            offer("P", power.real, "P = real(V x conj(I))", abs(power))
            offer("Qac", power.imag, "Qac = imag(V x conj(I))", abs(power))
        # If one power component exhausts S, the other must be zero. Otherwise
        # S and one component leave a +/- ambiguity; do not pick a sign from
        # the power triangle without additional information.
        for missing, present in (("Qac", "P"), ("P", "Qac")):
            if not has(missing) and has("S", present) and v["S"] == abs(v[present]):
                offer(missing, 0, f"{missing} = 0 when S = |{present}|")
        # Both signed components determine S and the power angle. A nonzero
        # voltage or current phasor then permits inversion of V*conj(I) to recover
        # the other phasor; the nonzero guards prevent division through zero.
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
        # Each component magnitude must fit inside apparent power. Use numeric
        # tolerance so a tiny rounding excess does not become a conflict.
        for key in ("P", "Qac"):
            if (
                has("S", key)
                and abs(v[key]) > v["S"]
                and not close(abs(v[key]), v["S"])
            ):
                self.note(f"Conflict: apparent power S is smaller than |{key}|.")

    # Propagate an equality from whichever side is available. If both are
    # already known, offer() verifies agreement instead of replacing either.
    def equal(self, a, b):
        for source, target in ((a, b), (b, a)):
            if self.has(source):
                self.offer(target, self.values[source], f"{target} = {source}")
                break

    # Apply out=a+b in any determined direction, such as Kirchhoff's voltage
    # sum around a series branch. Scale forward comparisons by the addends
    # so cancellation near zero does not trigger a spurious conflict.
    def sum_of(self, out, a, b):
        v, has, offer = self.values, self.has, self.offer
        if has(a, b):
            offer(out, v[a] + v[b], f"{out} = {a} + {b}", max(abs(v[a]), abs(v[b])))
        elif has(out, a):
            offer(b, v[out] - v[a], f"{b} = {out} - {a}")
        elif has(out, b):
            offer(a, v[out] - v[b], f"{a} = {out} - {b}")

    # Use the selected RC/RL step model to relate initial, final, and time-dependent values.
    # These closed-form quantity calculations are distinct from stepping a full component network.
    def transient(self):
        v, has, offer, model = self.values, self.has, self.offer, self.model
        self.magnitude("Vc", "Vcmag")
        self.magnitude("Il", "Ilmag")
        # Storage equations hold separately from a particular response topology:
        # Q=C*Vc, Ec=0.5*C*|Vc|**2, and El=0.5*L*|Il|**2. Magnitudes avoid
        # inventing a signed voltage/current from stored energy alone.
        if model == "rc" or set(v) & {"C", "Q", "Ec"}:
            self.product("Q", "C", "Vc")
            self.square_product("Ec", "C", "Vcmag", 0.5)
        if model == "rl" or set(v) & {"L", "El"}:
            self.square_product("El", "L", "Ilmag", 0.5)
        # The generic model integrates a constant capacitor current or inductor
        # voltage into a linear ramp. It does not assume the exponential response
        # of a resistor feeding the storage element.
        if model == "generic":
            for sample, initial, drive, part in (
                ("Vc", "V0", "Ic", "C"),
                ("Il", "I0", "Vl", "L"),
            ):
                if set(v) & {sample, initial, drive, part}:
                    self.ramp(sample, initial, drive, part)
        else:
            # For a series RC step, the capacitor reaches the source voltage and
            # branch current tends to zero. V denotes the resistor drop, while Vc
            # is the capacitor voltage at the requested time.
            if model == "rc":
                self.equal("Vfinal", "Vsrc")
                offer("Ifinal", 0, "Ifinal = 0 (capacitor at steady state)")
                self.drop()
                self.equal("I", "Ic")
                self.sum_of("Vsrc", "V", "Vc")
                self.decay("Vc", "V0", "Vfinal", "tauRC")
            # For a series RL step, inductor voltage tends to zero and final branch
            # current is Vsrc/R. The exponential state is Il, with initial value I0.
            else:
                self.product("Vsrc", "Ifinal", "R")
                if self.zero("Vsrc"):
                    offer("Ifinal", 0, "Ifinal = Vsrc / R = 0")
                offer("Vfinal", 0, "Vfinal = 0 (inductor at steady state)")
                self.drop()
                self.equal("I", "Il")
                self.sum_of("Vsrc", "V", "Vl")
                self.decay("Il", "I0", "Ifinal", "tauRL")
        # Apply resistor power where a resistor context exists. magnitude=False
        # prevents P=R*I**2 from choosing a sign for current; zero remains unique.
        if model != "generic" or has("R"):
            self.product("V", "I", "R")
            self.square_product("P", "R", "I", magnitude=False)
        if model != "generic":
            if self.zero("V"):
                offer("I", 0, "I = V / R = 0")
            self.square_product("P0", "R", "I0", magnitude=False)
        self.product("P", "V", "I")

    # At the initial instant, Vsrc=V0+I0*R for the selected series step.
    # V0 is the initial voltage across the capacitor or inductor. Reverse
    # the relationship only where a nonzero divisor determines a unique value.
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
            # With positive series R, equal source/initial voltage means zero initial
            # resistor current; it does not determine the resistance itself.
            if diff == 0:
                offer("I0", 0, "I0 = 0 when Vsrc = V0")
            elif v.get("R"):
                offer("I0", diff / v["R"], "I0 = (Vsrc - V0) / R")
            elif v.get("I0"):
                offer("R", diff / v["I0"], "R = (Vsrc - V0) / I0")

    # Integrate a constant drive: sample=initial+drive*t/part. The same shape
    # represents Vc=V0+Ic*t/C and Il=I0+Vl*t/L. Signed drive permits charging
    # or discharging without imposing a polarity on the stored state.
    def ramp(self, sample, initial, drive, part):
        v, has, offer = self.values, self.has, self.offer
        # At zero drive or zero elapsed time, sample equals initial even without
        # knowing C/L. Do not divide by a zero time to infer an arbitrary drive.
        if self.zero(drive) or self.zero("t"):
            self.equal(sample, initial)
        if has(drive, "t", part):
            # Known drive, time, and positive storage value determine the change.
            # Either endpoint then determines the other; scale the forward check by
            # both contributions to retain a meaningful cancellation tolerance.
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
        # Endpoint differences also permit inverse solves. With t>0 and no change,
        # drive must be zero, while C/L remains undetermined. Otherwise require
        # the divisor needed for the chosen rearrangement to be nonzero.
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

    # First-order response: sample=steady+(initial-steady)*exp(-t/tau).
    # RC uses capacitor voltage; RL uses inductor current. In addition to
    # forward evaluation, solve for an endpoint, elapsed time, or time constant
    # when the available data identify that quantity uniquely.
    def decay(self, sample, initial, steady, tau):
        v, has, offer = self.values, self.has, self.offer
        # With no transient amplitude the state is constant. This determines the
        # sample but cannot identify t or tau from a nonexistent decay.
        if has(initial, steady) and v[initial] == v[steady]:
            offer(sample, v[initial], f"{sample} = {initial} = {steady}")
        # At t=0 the storage state is its initial value, independent of tau.
        if self.zero("t"):
            self.equal(sample, initial)
        if has("t", tau):
            exponent = -v["t"] / v[tau]
            # a is the remaining transient fraction, and complement is 1-a.
            # expm1(x) evaluates exp(x)-1 accurately for tiny x, avoiding the loss of
            # a small early-time change when ordinary exp(x) rounds close to 1.
            a, complement = math.exp(exponent), -math.expm1(exponent)
            if has(initial, steady):
                # Near the initial state, add the small change using complement. Later,
                # evaluate steady+(initial-steady)*a directly so a small remaining tail
                # is not lost by subtracting two nearly equal large quantities.
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
            # Recovering the initial state divides by the remaining fraction. Skip
            # this inverse if exp(-t/tau) has underflowed to zero and no longer retains
            # information about the initial value.
            elif has(sample, steady) and a:
                offer(
                    initial,
                    v[steady] + (v[sample] - v[steady]) / a,
                    f"{initial} = {steady} + ({sample} - {steady}) x exp(t/{tau})",
                )
            # Recovering the final value divides by 1-a. At t=0 that is zero, so
            # a sample at the initial instant cannot reveal the steady state.
            elif has(sample, initial) and complement:
                offer(
                    steady,
                    v[initial] + (v[sample] - v[initial]) / complement,
                    f"{steady} = {initial} + ({sample} - {initial}) / (1 - exp(-t/{tau}))",
                )
        # Without both t and tau, derive the residual fraction exp(-t/tau) from
        # the endpoints and sample. One of t or tau must still be known to separate
        # the ratio into an elapsed time and a time constant.
        elif has(sample, initial, steady) and v[initial] != v[steady]:
            fraction = (v[sample] - v[steady]) / (v[initial] - v[steady])
            # For t>=0 and tau>0, a finite exponential residual is in (0,1]. Zero is
            # the asymptotic limit for this inversion; a negative residual or one
            # above 1 lies outside the finite forward step. Do not take an invalid log.
            if not 0 < fraction <= 1:
                self.note(f"Conflict: {sample} is outside the finite step response.")
            # An unchanged sample implies t=0 when the endpoints differ. Handle this
            # before log(1)=0 could appear as the divisor in an inverse tau formula.
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
