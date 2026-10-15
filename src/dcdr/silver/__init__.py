"""Silver layer: parse, QC, classify and sample ISD reports (spec §7.2).

Bump METHOD_VERSION whenever parse/QC/classify/sampling logic changes; silver files carry it,
and gold refuses to mix versions.
"""

METHOD_VERSION = "1.0.0"
