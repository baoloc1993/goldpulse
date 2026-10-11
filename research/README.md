# research/

These are offline scripts and not part of the application. They use numpy, pandas and scikit-learn through
`uv run --with`, so they are not project dependencies.

```bash
PYTHONPATH=src uv run python research/export_bars.py research/bars15.csv
uv run --no-project --with numpy --with pandas --with scikit-learn python research/price_methods.py research/bars15.csv
```

- `export_bars.py`: exports 15-minute bars from the database to CSV. `bars15.csv` (about 23 MB) is not committed.
- `features.py`, `price_methods.py`: compare price-only methods for predicting the next 15-minute direction
  (trained on 2012–2021, thresholds selected on 2022–2024, tested on hold-out data from 2025–2026).
- `train_logit.py`: trains the 15-minute logistic model and exports `data/models/logit15m_v1.json`.
- `parity_model.py`: checks the pure-Python production model bar by bar against the pandas reference.
- `paper_10usd.py`, `paper_filters.py`: backtest the paper-account rules.
- `btc_*.py`: the same workflow for BTCUSDT (fetch, features, strategies, model export, parity, replay).
