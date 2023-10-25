from abc import abstractmethod
import json
import logging
import os
import requests
import time
from remote_pdb import RemotePdb
from changedetectionio.content_fetcher import (
    Fetcher, visualselector_xpath_selectors,
    PageUnloadable, EmptyReply,
    ScreenshotUnavailable, Non200ErrorCodeReceived
)
from changedetectionio.browser_steps import nonContext

io_interface_context = None

class base_html_playwright_long_session(Fetcher):
    # By holding open a "live" browser context we can side step the buggy behavior

    # Some key things about browser_steps:browsersteps_live_ui:

    # - It creates a Playwright context and page object that stays open
    # - The `connect()` method sets a long default timeout to keep it open
    # - There are methods like `get_current_state()` to get screenshots/data on demand
    # - It handles events like the page closing to invalidate the context

    # To make the content fetcher code work this way:

    # - Create the context/page outside of the `run()` method so they persist
    # - Set a long timeout on the page to keep it open
    # - Add methods like `get_current_state()` that use the page when needed
    # - Listen for events like `page.on('close')` to handle the page closing
    # - Probably remove the puppeteer logic since this uses the Playwright API directly

    # So in summary:

    # - Persistent Playwright context/page < blueprint/browser_steps/nonContext.py
    # - Likely remove puppeteer code < likely 
    # - Long timeout to keep page open < easy, just set
    # - ~Methods to interact on demand~ < don't need 
    # - ~Handle events like page close~ < not sure we need
    context = None
    page = None
    render_extra_delay = 1
    stale = False
    # bump and kill this if idle after X sec
    age_start = 0

    fetcher_description = "Playwright chromium with longer session"
    if os.getenv("PLAYWRIGHT_DRIVER_URL"):
        fetcher_description += " via '{}'".format(os.getenv("PLAYWRIGHT_DRIVER_URL"))

    # use a special driver, maybe locally etc
    command_executor = os.getenv(
        "PLAYWRIGHT_BROWSERSTEPS_DRIVER_URL"
    )
    # if not..
    if not command_executor:
        command_executor = os.getenv(
            "PLAYWRIGHT_DRIVER_URL",
            'ws://playwright-chrome:3000'
        ).strip('"')

    browser_type = os.getenv("PLAYWRIGHT_BROWSER_TYPE", 'chromium').strip('"')

    # Configs for Proxy setup
    # In the ENV vars, is prefixed with "playwright_proxy_", so it is for example "playwright_proxy_server"
    playwright_proxy_settings_mappings = ['bypass', 'server', 'username', 'password']

    proxy = None

    def __init__(self, proxy_override=None):
        #RemotePdb('0.0.0.0', 4444).set_trace()

        super().__init__()
        # .strip('"') is going to save someone a lot of time when they accidently wrap the env value

        # If any proxy settings are enabled, then we should setup the proxy object
        proxy_args = {}
        for k in self.playwright_proxy_settings_mappings:
            v = os.getenv('playwright_proxy_' + k, False)
            if v:
                proxy_args[k] = v.strip('"')

        if proxy_args:
            self.proxy = proxy_args

        # allow per-watch proxy selection override
        if proxy_override:
            self.proxy = {'server': proxy_override}

        if self.proxy:
            # Playwright needs separate username and password values
            from urllib.parse import urlparse
            parsed = urlparse(self.proxy.get('server'))
            if parsed.username:
                self.proxy['username'] = parsed.username
                self.proxy['password'] = parsed.password

        self.age_start = time.time()
        # We keep the playwright session open for many minutes
        seconds_keepalive = int(os.getenv('BROWSERSTEPS_MINUTES_KEEPALIVE', 10)) * 60
        keepalive = "&timeout={}".format(((seconds_keepalive + 3) * 1000))        


        # Make context; should re use if called again
        if not io_interface_context:
            io_interface_context = nonContext.c_sync_playwright()
            # Start the Playwright context, which is actually a nodejs sub-process and communicates over STDIN/STDOUT pipes
            io_interface_context = io_interface_context.start()


        self.playwright_browser = io_interface_context.chromium.connect_over_cdp(
                os.getenv('PLAYWRIGHT_DRIVER_URL', '') + keepalive)
        if self.context is None:
            self.connect(proxy=self.proxy)

    # Connect and setup a new context
    def connect(self, proxy=None):
        # Should only get called once - test that
        keep_open = 1000 * 60 * 5
        now = time.time()

        # @todo handle multiple contexts, bind a unique id from the browser on each req?
        self.context = self.playwright_browser.new_context(
            # @todo
            #                user_agent=request_headers['User-Agent'] if request_headers.get('User-Agent') else 'Mozilla/5.0',
            #               proxy=self.proxy,
            # This is needed to enable JavaScript execution on GitHub and others
            bypass_csp=True,
            # Should never be needed
            accept_downloads=False,
            proxy=proxy
        )

        self.page = self.context.new_page()

        # self.page.set_default_navigation_timeout(keep_open)
        self.page.set_default_timeout(keep_open)
        # @todo probably this doesnt work
        self.page.on(
            "close",
            self.mark_as_closed,
        )
        # Listen for all console events and handle errors
        self.page.on("console", lambda msg: print(f"Browser steps console - {msg.type}: {msg.text} {msg.args}"))

        print("Time to browser setup", time.time() - now)
        self.page.wait_for_timeout(1 * 1000)    

    def mark_as_closed(self):
        print("Page closed, cleaning up..")

    @property
    def has_expired(self):
        if not self.page:
            return True

    def screenshot_step(self, step_n=''):
        screenshot = self.page.screenshot(type='jpeg', full_page=True, quality=85)

        if self.browser_steps_screenshot_path is not None:
            destination = os.path.join(self.browser_steps_screenshot_path, 'step_{}.jpeg'.format(step_n))
            logging.debug("Saving step screenshot to {}".format(destination))
            with open(destination, 'wb') as f:
                f.write(screenshot)

    def save_step_html(self, step_n):
        content = self.page.content()
        destination = os.path.join(self.browser_steps_screenshot_path, 'step_{}.html'.format(step_n))
        logging.debug("Saving step HTML to {}".format(destination))
        with open(destination, 'w') as f:
            f.write(content)

    def run_fetch_browserless_puppeteer(self,
            url,
            timeout,
            request_headers,
            request_body,
            request_method,
            ignore_status_codes=False,
            current_include_filters=None,
            is_binary=False):

        from pkg_resources import resource_string

        extra_wait_ms = (int(os.getenv("WEBDRIVER_DELAY_BEFORE_CONTENT_READY", 5)) + self.render_extract_delay) * 1000

        self.xpath_element_js = self.xpath_element_js.replace('%ELEMENTS%', visualselector_xpath_selectors)
        code = resource_string(__name__, "res/puppeteer_fetch.js").decode('utf-8')
        # In the future inject this is a proper JS package
        code = code.replace('%xpath_scrape_code%', self.xpath_element_js)
        code = code.replace('%instock_scrape_code%', self.instock_data_js)

        from requests.exceptions import ConnectTimeout, ReadTimeout
        wait_browserless_seconds = 240

        browserless_function_url = os.getenv('BROWSERLESS_FUNCTION_URL')
        from urllib.parse import urlparse
        if not browserless_function_url:
            # Convert/try to guess from PLAYWRIGHT_DRIVER_URL
            o = urlparse(os.getenv('PLAYWRIGHT_DRIVER_URL'))
            browserless_function_url = o._replace(scheme="http")._replace(path="function").geturl()


        # Append proxy connect string
        if self.proxy:
            import urllib.parse
            # Remove username/password if it exists in the URL or you will receive "ERR_NO_SUPPORTED_PROXIES" error
            # Actual authentication handled by Puppeteer/node
            o = urlparse(self.proxy.get('server'))
            proxy_url = urllib.parse.quote(o._replace(netloc="{}:{}".format(o.hostname, o.port)).geturl())
            browserless_function_url = f"{browserless_function_url}&--proxy-server={proxy_url}&dumpio=true"


        try:
            amp = '&' if '?' in browserless_function_url else '?'
            response = requests.request(
                method="POST",
                json={
                    "code": code,
                    "context": {
                        # Very primitive disk cache - USE WITH EXTREME CAUTION
                        # Run browserless container  with -e "FUNCTION_BUILT_INS=[\"fs\",\"crypto\"]"
                        'disk_cache_dir': os.getenv("PUPPETEER_DISK_CACHE", False), # or path to disk cache ending in /, ie /tmp/cache/
                        'execute_js': self.webdriver_js_execute_code,
                        'extra_wait_ms': extra_wait_ms,
                        'include_filters': current_include_filters,
                        'req_headers': request_headers,
                        'screenshot_quality': int(os.getenv("PLAYWRIGHT_SCREENSHOT_QUALITY", 72)),
                        'url': url,
                        'user_agent': request_headers.get('User-Agent', 'Mozilla/5.0'),
                        'proxy_username': self.proxy.get('username','') if self.proxy else False,
                        'proxy_password': self.proxy.get('password', '') if self.proxy else False,
                        'no_cache_list': [
                            'twitter',
                            '.pdf'
                        ],
                        # Could use https://github.com/easylist/easylist here, or install a plugin
                        'block_url_list': [
                            'adnxs.com',
                            'analytics.twitter.com',
                            'doubleclick.net',
                            'google-analytics.com',
                            'googletagmanager',
                            'trustpilot.com'
                        ]
                    }
                },
                # @todo /function needs adding ws:// to http:// rebuild this
                url=browserless_function_url+f"{amp}--disable-features=AudioServiceOutOfProcess&dumpio=true&--disable-remote-fonts",
                timeout=wait_browserless_seconds)

        except ReadTimeout:
            raise PageUnloadable(url=url, status_code=None, message=f"No response from browserless in {wait_browserless_seconds}s")
        except ConnectTimeout:
            raise PageUnloadable(url=url, status_code=None, message=f"Timed out connecting to browserless, retrying..")
        else:
            # 200 Here means that the communication to browserless worked only, not the page state
            if response.status_code == 200:
                import base64

                x = response.json()
                if not x.get('screenshot'):
                    # https://github.com/puppeteer/puppeteer/blob/v1.0.0/docs/troubleshooting.md#tips
                    # https://github.com/puppeteer/puppeteer/issues/1834
                    # https://github.com/puppeteer/puppeteer/issues/1834#issuecomment-381047051
                    # Check your memory is shared and big enough
                    raise ScreenshotUnavailable(url=url, status_code=None)

                if not x.get('content', '').strip():
                    raise EmptyReply(url=url, status_code=None)

                if x.get('status_code', 200) != 200 and not ignore_status_codes:
                    raise Non200ErrorCodeReceived(url=url, status_code=x.get('status_code', 200), page_html=x['content'])

                self.content = x.get('content')
                self.headers = x.get('headers')
                self.instock_data = x.get('instock_data')
                self.screenshot = base64.b64decode(x.get('screenshot'))
                self.status_code = x.get('status_code')
                self.xpath_data = x.get('xpath_data')

            else:
                # Some other error from browserless
                raise PageUnloadable(url=url, status_code=None, message=response.content.decode('utf-8'))

    def run(self,
            url,
            timeout,
            request_headers,
            request_body,
            request_method,
            ignore_status_codes=False,
            current_include_filters=None,
            is_binary=False):

        # For now, USE_EXPERIMENTAL_PUPPETEER_FETCH is not supported by watches with BrowserSteps (for now!)
        has_browser_steps = self.browser_steps and list(filter(
                lambda s: (s['operation'] and len(s['operation']) and s['operation'] != 'Choose one' and s['operation'] != 'Goto site'),
                self.browser_steps))

        if not has_browser_steps:
            if os.getenv('USE_EXPERIMENTAL_PUPPETEER_FETCH'):
                # Temporary backup solution until we rewrite the playwright code
                return self.run_fetch_browserless_puppeteer(
                    url,
                    timeout,
                    request_headers,
                    request_body,
                    request_method,
                    ignore_status_codes,
                    current_include_filters,
                    is_binary)

        from playwright.sync_api import sync_playwright
        import playwright._impl._api_types

        self.delete_browser_steps_screenshots()
        response = None
        with sync_playwright() as p:
            browser_type = getattr(p, self.browser_type)

            # Seemed to cause a connection Exception even tho I can see it connect
            # self.browser = browser_type.connect(self.command_executor, timeout=timeout*1000)
            # 60,000 connection timeout only
            browser = browser_type.connect_over_cdp(self.command_executor, timeout=60000)

            # Set user agent to prevent Cloudflare from blocking the browser
            # Use the default one configured in the App.py model that's passed from fetch_site_status.py
            context = browser.new_context(
                user_agent=request_headers.get('User-Agent', 'Mozilla/5.0'),
                proxy=self.proxy,
                # This is needed to enable JavaScript execution on GitHub and others
                bypass_csp=True,
                # Should be `allow` or `block` - sites like YouTube can transmit large amounts of data via Service Workers
                service_workers=os.getenv('PLAYWRIGHT_SERVICE_WORKERS', 'allow'),
                # Should never be needed
                accept_downloads=False
            )

            self.page = context.new_page()
            if len(request_headers):
                context.set_extra_http_headers(request_headers)

                self.page.set_default_navigation_timeout(90000)
                self.page.set_default_timeout(90000)

                # Listen for all console events and handle errors
                self.page.on("console", lambda msg: print(f"Playwright console: Watch URL: {url} {msg.type}: {msg.text} {msg.args}"))

            # Goto page
            try:
                # Wait_until = commit
                # - `'commit'` - consider operation to be finished when network response is received and the document started loading.
                # Better to not use any smarts from Playwright and just wait an arbitrary number of seconds
                # This seemed to solve nearly all 'TimeoutErrors'
                response = self.page.goto(url, wait_until='commit')
            except playwright._impl._api_types.Error as e:
                # Retry once - https://github.com/browserless/chrome/issues/2485
                # Sometimes errors related to invalid cert's and other can be random
                print("Content Fetcher > retrying request got error - ", str(e))
                time.sleep(1)
                response = self.page.goto(url, wait_until='commit')

            except Exception as e:
                print("Content Fetcher > Other exception when page.goto", str(e))
                context.close()
                browser.close()
                raise PageUnloadable(url=url, status_code=None, message=str(e))

            # Execute any browser steps
            try:
                extra_wait = int(os.getenv("WEBDRIVER_DELAY_BEFORE_CONTENT_READY", 5)) + self.render_extract_delay
                self.page.wait_for_timeout(extra_wait * 1000)

                if self.webdriver_js_execute_code is not None and len(self.webdriver_js_execute_code):
                    self.page.evaluate(self.webdriver_js_execute_code)

            except playwright._impl._api_types.TimeoutError as e:
                context.close()
                browser.close()
                # This can be ok, we will try to grab what we could retrieve
                pass
            except Exception as e:
                print("Content Fetcher > Other exception when executing custom JS code", str(e))
                context.close()
                browser.close()
                raise PageUnloadable(url=url, status_code=None, message=str(e))

            if response is None:
                context.close()
                browser.close()
                print("Content Fetcher > Response object was none")
                raise EmptyReply(url=url, status_code=None)

            # Run Browser Steps here
            self.iterate_browser_steps()

            extra_wait = int(os.getenv("WEBDRIVER_DELAY_BEFORE_CONTENT_READY", 5)) + self.render_extract_delay
            time.sleep(extra_wait)

            self.content = self.page.content()
            self.status_code = response.status
            if len(self.page.content().strip()) == 0:
                context.close()
                browser.close()
                print("Content Fetcher > Content was empty")
                raise EmptyReply(url=url, status_code=response.status)

            self.status_code = response.status
            self.headers = response.all_headers()

            # So we can find an element on the page where its selector was entered manually (maybe not xPath etc)
            if current_include_filters is not None:
                self.page.evaluate("var include_filters={}".format(json.dumps(current_include_filters)))
            else:
                self.page.evaluate("var include_filters=''")

            self.xpath_data = self.page.evaluate(
                "async () => {" + self.xpath_element_js.replace('%ELEMENTS%', visualselector_xpath_selectors) + "}")
            self.instock_data = self.page.evaluate("async () => {" + self.instock_data_js + "}")

            # Bug 3 in Playwright screenshot handling
            # Some bug where it gives the wrong screenshot size, but making a request with the clip set first seems to solve it
            # JPEG is better here because the screenshots can be very very large

            # Screenshots also travel via the ws:// (websocket) meaning that the binary data is base64 encoded
            # which will significantly increase the IO size between the server and client, it's recommended to use the lowest
            # acceptable screenshot quality here
            try:
                # The actual screenshot
                self.screenshot = self.page.screenshot(type='jpeg', full_page=True,
                                                       quality=int(os.getenv("PLAYWRIGHT_SCREENSHOT_QUALITY", 72)))
            except Exception as e:
                context.close()
                browser.close()
                raise ScreenshotUnavailable(url=url, status_code=None)

            context.close()
            browser.close()