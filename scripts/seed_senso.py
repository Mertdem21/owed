"""Upload the merchant policies to Senso (one document per policy section).

    uv run python -m scripts.seed_senso
"""
from owed.integrations import senso


def main() -> None:
    if not senso.enabled():
        raise SystemExit("SENSO_API_KEY is not set in .env")
    print("Uploading policies to Senso ...")
    ids = senso.seed_policies(wait=True)
    print(f"Done: {len(ids)} policy sections in Senso. Test with: uv run python -m owed.cli smoke senso")


if __name__ == "__main__":
    main()
