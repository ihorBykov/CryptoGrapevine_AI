# Debugging components independently

Run all commands from the project root after activating the environment:

```bash
source venv/bin/activate
```

## X / Apify

Fetch new posts and save the normalized result as JSON:

```bash
python scraper_x.py --fetch --max-items 5 --output data/x_posts.json
```

By default, an independent run does not update `.seen_tweets.json`, so the experiment does not affect the next `main.py` run. Add `--mark-seen` to update this file explicitly.

Replay a saved result without an Apify request:

```bash
python scraper_x.py --input data/x_posts.json
```

`main.py` also saves each successful collection result to `data/latest_x_posts.json`.

## Brain / Gemini

Run the local filter and the complete analysis flow for one text:

```bash
python brain.py --text "Victim was hacked: 0x0123456789012345678901234567890123456789"
```

Print the exact system prompt and user content without calling Gemini:

```bash
python brain.py --text "Text to inspect" --show-prompt
```

Run a specific model and bypass the local filter:

```bash
python brain.py --text "Text to inspect" --skip-filter --model gemini-2.5-flash-lite
```

Repeat `--model` to define a custom fallback order. Replay one post from an archive without Apify:

```bash
python brain.py --posts-file data/x_posts.json --post-index 0
```

## Wallet review

Review one address through the same dispatcher used by `main.py`:

```bash
python wallet_review.py --blockchain bitcoin --address 1BoatSLRHtKNngkdXEeobR76b53LETtpyT
python wallet_review.py --blockchain ethereum --address 0x0123456789012345678901234567890123456789
python wallet_review.py --blockchain tron --address TWd4WrZ9wn84f5x1hZhL4DHvk738ns5jwb
```

The result is printed as JSON. Sample data can test the analyzer logic without network requests:

```bash
python btc_wallet_analyzer.py --test
python eth_wallet_analyzer.py --test
python tron_usdt_analyzer.py --test
```

For TRON, full analysis completes when the USDT or TRX transfer history ends with a short final page before 10,000 records. The TronScan `rangeTotal` and `total` fields are not used for address-filtered requests. If 10,000 records are loaded without reaching the end, the address is not classified as an exchange: the report includes available TRX and USDT balances, the TronScan account transaction count, and `requires_manual_verification`.

## Tavily public-source search

Add `TAVILY_API_KEY` to `.env`. Search returns up to five sources, uses `basic` depth by default, and caches each address-and-context combination for seven days:

```bash
python tavily_wallet_search.py 0x3c4f6ca9bea79432eaf8d98c3be4570064f1fde8 \
  --context "M1llionz USDT freeze"
```

Test an address with many public references:

```bash
python tavily_wallet_search.py bc1qdlld6antmv4xug242ed83q7k4rqw50cwfns38szx4qu2f4jwaxxsuhwxxr
```

For a detected incident, `main.py` reviews every extracted wallet and runs Tavily in parallel for up to two priority addresses. It saves the detailed Markdown report to `data/reports/` and sends it to Telegram as an attachment.
