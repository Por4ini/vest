from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time

def bypass_aws_waf(url):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        )
        page = context.new_page()
        Stealth().apply_stealth_sync(page)  # patches automation detection signals

        page.goto(url, wait_until="networkidle")

        # Wait for the WAF challenge to resolve
        # AWS WAF challenges typically complete within 5 seconds
        time.sleep(5)

        # Extract the aws-waf-token cookie
        cookies = context.cookies()
        waf_token = None
        for cookie in cookies:
            if "aws-waf-token" in cookie["name"]:
                waf_token = cookie["value"]
                break
            print(f"Cookie found: {cookie['name']} = {cookie['value']}")
        content = page.content()
        browser.close()
        return waf_token, content

token, html = bypass_aws_waf("https://connect.vestwell.com/register")
if token:
    print(f"Got WAF token: {token[:500]}...")
    print(f"ys: {int(time.time() * 1000)}")
    print(f"d: connect.vestwell.com")
    print(f"h: register")
else:
    print("No WAF token found — site may not use AWS WAF")
