"""Spain-wide registries.

**This module is empty, and that is a known gap rather than an oversight.**

The plan allocated the Spanish national registries to module M4. Of that module only the
INE/GEOSTAT population grid was built, and it lives in ``population.py`` because it
writes to ``pop_grid`` rather than ``assets``. The four registries below were never
implemented, so outside Catalonia the service returns OpenStreetMap only and reports that
honestly through ``coverage_regime`` on every response.

What belongs here, in rough order of value for fire triage:

===========================  ====================================================
Catálogo Nacional de         Bed counts per hospital. Would lift healthcare
Hospitales (MSAN)            capacity from a class default to a published figure,
                             which is the single largest source of error in the
                             people-at-risk estimate today.
REGCESS                      National register of health centres - clinics and day
                             centres that the Catalan RESES feed does not carry
                             outside Catalonia.
CSIC care-home register      Residential care homes with places. The highest
                             evacuation priority in the taxonomy, currently
                             visible only where OSM happens to tag one.
National school registry     Enrolment per school outside Catalonia.
===========================  ====================================================

Adding one is a self-contained change, not an architectural one:

1. Subclass ``Connector`` here, set ``meta``, ``tier = Tier.RESIDENT`` and
   ``coverage = SPAIN``, and decorate with ``@register``.
2. Implement ``fetch()`` (yield raw records) and ``normalise()`` (yield ``RawAsset``).
   The base class handles ids, geocoding, batching, provenance and the run log.
3. Add the source id to ``SOURCE_PRIORITY`` in ``services/conflation.py`` — the four
   ids above are **already listed there**, so a new connector conflates with the Catalan
   and OSM records on the first run without further work.

``catalunya.py`` has six worked examples against a Socrata API; ``population.py`` shows a
connector that overrides ``ingest()`` to write somewhere other than ``assets``.

Nothing else needs to change: the registry discovers connectors by import, and
``es/__init__.py`` already imports this module.
"""
