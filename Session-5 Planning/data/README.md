# Data Directory

This directory holds the local scheme dataset.

## Primary source

Download the HuggingFace dataset:

```bash
huggingface-cli download shrijayan/gov_myscheme \
    --repo-type dataset \
    --local-dir .
```

Expected files after download:
- `myscheme.csv` or `myscheme.json` (the structured dataset)
- PDFs for individual schemes (optional, used for richer detail if needed)

The loader in `src/scheme_data.py` will auto-detect either file format.

## Fallback

If you don't download the dataset, the project falls back to a built-in seed
of 10 major central schemes in `src/scheme_data.py`. This is enough for
demos but not for production-grade coverage.

## License

The HuggingFace dataset is derived from publicly available government data
on [myscheme.gov.in](https://www.myscheme.gov.in/). Ensure your usage
complies with government data usage policies.
