"""Quantity names, labels and base SI units shared by the compiler and UI."""
# The compiler, equation rules, and UI share these keys, labels, and base units.
# A display label can change independently of the key used to store or derive a quantity.

# Each entry maps a stable quantity key to (display label, unit token).
# The compiler uses the unit token when parsing; reports use the label and
# unit when formatting. These are catalog units, including degrees for
# angles below, rather than a list restricted to SI base units.
#
# Value domains and equation rules live in equations.py. This catalog
# describes available quantities without itself enforcing signs, ranges,
# or whether enough inputs exist to derive a value.
_COMMON = {
    "V": ("Voltage", "V"),
    "I": ("Current", "A"),
    "R": ("Resistance", "ohm"),
    "P": ("Power", "W"),
    "C": ("Capacitance", "F"),
    "L": ("Inductance", "H"),
    # Scalar time-constant relationships are tauRC=R*C and tauRL=L/R.
    # Their availability does not by itself specify a connected circuit topology.
    "tauRC": ("RC time constant", "s"),
    "tauRL": ("RL time constant", "s"),
}

# Each analysis mode starts with **_COMMON, a shallow copy of its entries.
# Later entries can override a shared label without changing _COMMON.
# Mode membership also lets the quantity solver reject unavailable inputs.
# An empty unit string denotes a dimensionless quantity, not a missing unit.
QUANTITIES = {
    # DC values use signed real voltage/current. Keep magnitudes separate so
    # a power calculation can use |V|/|I| without inventing a missing polarity.
    "dc": {
        **_COMMON,
        "Vmag": ("Voltage magnitude", "V"),
        # Imag means current magnitude |I|, not the imaginary component of I.
        "Imag": ("Current magnitude", "A"),
        # Transfer quantities obey E=P*t and Q=I*t. Unit C here means coulombs;
        # the quantity key C above instead means capacitance measured in farads.
        "E": ("Energy transferred", "J"),
        "Q": ("Charge transferred", "C"),
        # Qc is stored capacitor charge C*V, distinct from transferred charge Q.
        # Ec and El below are stored energies, 0.5*C*|V|**2 and 0.5*L*|I|**2.
        "Qc": ("Capacitor charge", "C"),
        "t": ("Elapsed time", "s"),
        "Ec": ("Capacitor energy", "J"),
        "El": ("Inductor energy", "J"),
        # Track input and useful-output energy/power separately. The efficiency
        # rules relate them through Eout=eta*Ein and Pout=eta*Pin.
        "Ein": ("Input energy", "J"),
        "Eout": ("Useful output energy", "J"),
        "Pin": ("Input power", "W"),
        "Pout": ("Useful output power", "W"),
        # Efficiency is a fraction: 0.8 means 80%. The scalar input parser can
        # also convert a dimensionless percentage into this representation.
        "eta": ("Efficiency (0–1)", ""),
    },
    # AC uses complex RMS phasors and signed reactance/power conventions.
    # Override the common V/I/P labels to make those meanings explicit.
    "ac": {
        **_COMMON,
        "V": ("Voltage phasor (RMS)", "V"),
        "I": ("Current phasor (RMS)", "A"),
        "P": ("Real power", "W"),
        # Z=R+jX is complex impedance; Zmag=|Z| is its real magnitude. X is the
        # signed imaginary part, which distinguishes inductive and capacitive behavior.
        "Z": ("Impedance", "ohm"),
        "Zmag": ("Impedance magnitude", "ohm"),
        "X": ("Reactance", "ohm"),
        # Ideal component reactances use XL=w*L and XC=-1/(w*C). XC is signed
        # negative for finite positive C/frequency; ZL=j*XL and ZC=j*XC.
        "XL": ("Inductive reactance", "ohm"),
        "XC": ("Capacitive reactance (signed)", "ohm"),
        "ZL": ("Inductor impedance", "ohm"),
        "ZC": ("Capacitor impedance", "ohm"),
        # These are the RMS magnitudes |V| and |I|. As in DC, Imag is a magnitude
        # key rather than an instruction to extract the imaginary part.
        "Vmag": ("RMS voltage magnitude", "V"),
        "Imag": ("RMS current magnitude", "A"),
        # For sinusoidal quantities, peak=sqrt(2)*RMS and peak-to-peak=2*peak.
        # All representations retain the same voltage/current unit tokens.
        "Vpk": ("Peak voltage", "V"),
        "Ipk": ("Peak current", "A"),
        "Vpp": ("Peak-to-peak voltage", "V"),
        "Ipp": ("Peak-to-peak current", "A"),
        # Angles are stored in degrees. phaseZ is the impedance angle, while
        # dphi is phaseV-phaseI; a phase difference does not fix an absolute phase.
        "phaseV": ("Voltage phase", "deg"),
        "phaseI": ("Current phase", "deg"),
        "phaseZ": ("Impedance phase", "deg"),
        "dphi": ("Phase difference", "deg"),
        # The signed-delay rule uses dphi=-360*f*dt, so positive delay corresponds
        # to negative phase shift in that relation.
        "dt": ("Signed delay", "s"),
        # Electrical rate can be specified as frequency f, period T=1/f, or
        # angular frequency w=2*pi*f. Capital T is a period; lowercase t in the
        # other catalogs is elapsed time.
        "f": ("Frequency", "Hz"),
        "T": ("Period", "s"),
        "w": ("Angular frequency", "rad/s"),
        # Complex power is V*conj(I)=P+jQac for RMS phasors. S is its magnitude
        # (VA); P is real power (W) and Qac is signed reactive power (var).
        # Qac avoids confusing reactive power with the charge key Q in other modes.
        "S": ("Apparent power", "VA"),
        "Qac": ("Reactive power", "var"),
        # Power factor is the signed ratio P/S=cos(dphi) where defined. The solver
        # permits -1..1; it is not automatically replaced by an absolute magnitude.
        "pf": ("Power factor", ""),
        # In the motor relationships, eta converts electrical real power P into
        # mechanical output Pmech. It remains a dimensionless fraction in 0..1.
        "eta": ("Motor efficiency (0–1)", ""),
        # Convert electrical angular rate to shaft rate with
        # shaftW=w*speedRatio, then relate output power by Pmech=torque*shaftW.
        # The shaft/electrical speed ratio is supplied or inferred explicitly.
        "speedRatio": ("Shaft / electrical speed ratio", ""),
        "shaftW": ("Shaft angular speed", "rad/s"),
        "Pmech": ("Mechanical output power", "W"),
        "torque": ("Shaft torque", "Nm"),
    },
    # This catalog supports closed-form quantity responses evaluated at t.
    # For selected series RC/RL step models, inherited V is the resistor
    # voltage drop and I is series current; storage-element states use Vc/Il.
    "transient": {
        **_COMMON,
        "t": ("Elapsed time", "s"),
        # Vsrc is the applied step-source level. V0/I0 describe initial voltage
        # and current; Vfinal/Ifinal describe their steady-state endpoints.
        "Vsrc": ("Step source voltage", "V"),
        "V0": ("Initial voltage", "V"),
        # For a series RC step, capacitor Vfinal=Vsrc and Ifinal=0. For a series
        # RL step, inductor Vfinal=0 and Ifinal=Vsrc/R. The selected model gives
        # the generic endpoint names their particular storage-element meaning.
        "Vfinal": ("Final voltage", "V"),
        "I0": ("Initial current", "A"),
        "Ifinal": ("Final current", "A"),
        # Vc and Ic are signed capacitor voltage/current at elapsed time t.
        # Vcmag is |Vc|, used for stored-energy inference without choosing polarity.
        "Vc": ("Capacitor voltage at t", "V"),
        "Vcmag": ("Capacitor voltage magnitude", "V"),
        "Ic": ("Capacitor current at t", "A"),
        # Vl and Il are signed inductor voltage/current at t; Ilmag=|Il|.
        # Constant-voltage ramps and series RL steps use these same state keys.
        "Vl": ("Inductor voltage at t", "V"),
        "Il": ("Inductor current at t", "A"),
        "Ilmag": ("Inductor current magnitude", "A"),
        # P0 is initial resistor power R*I0**2. The inherited P key represents
        # power at the evaluated time, so initial and sampled power stay distinct.
        "P0": ("Initial resistor power", "W"),
        # Energy uses the sampled storage state: Ec=0.5*C*Vcmag**2 and
        # El=0.5*L*Ilmag**2. Squaring removes sign, so energy alone cannot
        # determine the polarity of Vc or Il.
        "Ec": ("Capacitor energy", "J"),
        "El": ("Inductor energy", "J"),
        # In transient mode Q specifically means capacitor charge C*Vc. DC uses
        # Qc for stored capacitor charge and Q for transferred charge instead.
        "Q": ("Capacitor charge", "C"),
    },
}
