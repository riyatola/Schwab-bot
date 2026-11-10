"""
Manual OAuth token exchange script — use this when the automated browser flow
doesn't work (e.g. running on a remote server / sandbox without a browser).

Usage:
1. Run scanner.py --auth-only first to get the authorize URL OR copy the URL printed below
2. Open the URL in a browser, authorize, and copy the full redirected URL
3. Run this script:
   python manual_auth.py "https://127.0.0.1/?code=..."
"""
import sys
import yaml
import logging

def main():
    if len(sys.argv) < 2:
        print("ERROR: Missing callback URL argument!")
        print("Usage: python manual_auth.py '<full callback URL>'")
        print("")
        print("Example:")
        print('python manual_auth.py "https://127.0.0.1/?code=ABC...&session=XYZ..."')
        sys.exit(1)

    callback_url = sys.argv[1]
    print(f"Callback URL received: {callback_url[:80]}...")

    with open("config.yaml", "r") as f:
        cfg = yaml.safe_load(f)

    schwab_cfg = cfg["schwab"]
    token_path = schwab_cfg.get("token_path", "./tokens.json")

    # Setup minimal logger (required by Tokens)
    logger = logging.getLogger("manual_auth")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)

    print(f"Using app key: {schwab_cfg['app_key'][:8]}...")
    print(f"Token path: {token_path}")

    # Import Tokens class from schwabdev
    try:
        from schwabdev.client import Tokens
    except ImportError:
        try:
            from schwabdev.tokens import Tokens
        except ImportError:
            print("ERROR: schwabdev not installed. Run: pip install schwabdev")
            sys.exit(1)

    # This callback simply returns the user-provided URL instead of
    # opening a browser / waiting for stdin.
    auth_called = {"flag": False}
    def provide_auth_url(unused_auth_url):
        auth_called["flag"] = True
        return callback_url

    print("\nExchanging authorization code for tokens...")
    try:
        # Initialize Tokens — when loaded DB is empty/expired, it will call
        # our provide_auth_url callback and exchange the code for tokens.
        tokens = Tokens(
            app_key=schwab_cfg["app_key"],
            app_secret=schwab_cfg["app_secret"],
            callback_url=schwab_cfg.get("callback_url", "https://127.0.0.1"),
            logger=logger,
            tokens_db=token_path,
            call_for_auth=provide_auth_url,
            open_browser_for_auth=False,
        )
    except Exception as e:
        print(f"\n❌ ERROR: Token exchange failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n✅ SUCCESS!")
    print(f"   Tokens saved to: {token_path}")
    print(f"   Refresh token issued: {tokens._refresh_token_issued}")
    print(f"   Access token issued:  {tokens._access_token_issued}")
    print("\nYou can now run: python scanner.py")


if __name__ == "__main__":
    main()
