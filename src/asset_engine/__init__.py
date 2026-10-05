"""Store screenshots, the Play feature graphic and print flyers, composed from an app's definitions.

The engine is the code; an app keeps its definitions — the JSON layouts, captions, captures, brand
faces — and installs them as an ``AssetProject`` (``project.py``) before it calls anything here.

- ``store``: localized App Store / Play frames per device and store page, and the feature graphic.
- ``flyers``: print flyers in millimetres, with the QR codes and the forwarding pages behind them.
"""
