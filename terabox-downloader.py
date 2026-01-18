import os
import re
import json
import time
import requests
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from requests.adapters import HTTPAdapter
from tqdm import tqdm

WORKERS = 32

class TeraDownloader:
    def __init__(self, cookie_string):
        self.base_url = "https://www.1024terabox.com"
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Connection': 'keep-alive',
        })
        self.set_cookies(cookie_string)
        self.js_token = ""
        self.auth = {}

    def set_cookies(self, cookie_str):
        jar = requests.cookies.RequestsCookieJar()
        cookie_str = cookie_str.replace("Cookie: ", "")
        for c in cookie_str.split(';'):
            if '=' in c:
                k, v = c.strip().split('=', 1)
                jar.set(k, v, domain='.1024terabox.com')
        self.session.cookies.update(jar)

    def get_token(self):
        """
        Fetches and CLEANS the jsToken.
        """
        print("[*] Fetching Token...")
        try:
            r = self.session.get(f"{self.base_url}/main", timeout=10)
            
            # Extract the raw jsToken field from templateData
            tdata_match = re.search(r'<script>var templateData = (.*?);</script>', r.text)
            raw_token = ""
            
            if tdata_match:
                try:
                    data = json.loads(tdata_match.group(1))
                    raw_token = data.get('jsToken', '')
                except: pass
            
            # If not in templateData, check for direct fn call
            if not raw_token:
                fn_match = re.search(r'fn\("%28%22(.*?)%22%29"\)', r.text)
                if fn_match:
                    raw_token = fn_match.group(1)

            if "fn(" in raw_token or "%28" in raw_token:
                token_match = re.search(r'%22([A-F0-9]{30,})%22', raw_token, re.IGNORECASE)
                if token_match:
                    self.js_token = token_match.group(1)
                else:
                    token_match = re.search(r'"([A-F0-9]{30,})"', raw_token, re.IGNORECASE)
                    if token_match:
                        self.js_token = token_match.group(1)
            else:
                self.js_token = raw_token

            if self.js_token:
                print(f"[+] Token Cleaned: {self.js_token[:15]}...")
            else:
                print("[-] Could not extract clean token.")

        except Exception as e:
            print(f"[!] Token Error: {e}")

    def verify_password(self, surl):
        pwd = input(">> Enter Password: ").strip()
        url = f"{self.base_url}/share/verify"
        params = {'app_id': '250528', 'jsToken': self.js_token, 'surl': surl}
        data = {'pwd': pwd, 'vcode': '', 'vcode_str': ''}
        self.session.headers['Referer'] = f'{self.base_url}/sharing/init?surl={surl}'
        
        r = self.session.post(url, params=params, data=data)
        if r.json().get('errno') == 0:
            print("[+] Password Accepted.")
            self.session.cookies.set('BOXCLND', r.json().get('randsk'), domain='.1024terabox.com')
            return True
        print("[-] Wrong Password.")
        return False

    def get_metadata(self, url):
        # Clean URL to get key
        key = url.strip().split()[0]
        if "/s/" in key: key = key.split("/s/")[-1].split("?")[0]
        if "surl=" in key: key = key.split("surl=")[-1].split("&")[0]
        
        # surl usually requires stripping '1' if key is long
        surl = key[1:] if key.startswith('1') and len(key) > 20 else key
        
        if not self.js_token: self.get_token()

        info_url = f"{self.base_url}/api/shorturlinfo"
        params = {'app_id': '250528', 'shorturl': key, 'root': '1', 'jsToken': self.js_token}
        
        r = self.session.get(info_url, params=params)
        data = r.json()

        if data.get('errno') == -9:
            if not self.verify_password(surl): return None
            r = self.session.get(info_url, params=params)
            data = r.json()

        if data.get('errno') != 0:
            print(f"[-] Metadata Error: {data}")
            return None

        self.auth = {
            'shareid': data['shareid'], 'uk': data['uk'], 'sign': data['sign'],
            'timestamp': data['timestamp'], 'sekey': urllib.parse.unquote(data['randsk']),
            'surl': surl
        }
        
        root = data['list'][0]
        # Store root info
        is_file = (str(root.get('isdir', '1')) == "0")
        return root['path'], is_file

    def fetch_list(self, path, is_single_file=False):
        """Fetches file list. Handles Root vs Folder logic correctly."""
        
        ref_path = f"&path={urllib.parse.quote(path)}" if path != "/" and not is_single_file else ""
        self.session.headers['Referer'] = f'{self.base_url}/sharing/link?surl={self.auth["surl"]}{ref_path}'

        params = {
            'app_id': '250528', 'jsToken': self.js_token, 'page': 1, 'num': 1000,
            'order': 'name', 'desc': 1, 'shorturl': self.auth['surl']
        }
        params.update({k:v for k,v in self.auth.items() if k != 'surl'})


        if is_single_file:
            params['root'] = '1'
        else:
            params['dir'] = path
            if path == "/": params['root'] = '1'

        try:
            r = self.session.get(f"{self.base_url}/share/list", params=params)
            data = r.json()
            if data.get('errno') != 0:
                print(f"[!] API Error on {path}: {data}")
                return []
            return data.get('list', [])
        except Exception as e:
            print(f"[!] Network Error: {e}")
            return []

    def crawl(self, path):
        """Recursively finds all files."""
        items = self.fetch_list(path, False)
        files = []
        for i in items:
            if str(i.get('isdir', '0')) == "1":
                print(f"[*] Crawling Folder: {i['server_filename']}...")
                files.extend(self.crawl(i['path']))
            else:
                files.append(i)
        return files

    def download(self, file_obj, max_workers=WORKERS):
        name = file_obj['server_filename']
        size = int(file_obj['size'])
        dlink = file_obj['dlink']
        
        # Local Path
        local_path = file_obj['path'].lstrip('/') if 'path' in file_obj else name
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.exists(local_dir):
            os.makedirs(local_dir, exist_ok=True)

        if os.path.exists(local_path):
            if os.path.getsize(local_path) == size:
                print(f"[#] Skipping {name} (Complete)")
                return

        print(f"\n[+] Downloading: {name} ({size/1024/1024:.2f} MB)")
        
        with open(local_path, "wb") as f: f.truncate(size)

        chunk_size = size // max_workers
        ranges = [(i*chunk_size, (i+1)*chunk_size-1) for i in range(max_workers)]
        ranges[-1] = (ranges[-1][0], size - 1)

        pbar = tqdm(total=size, unit='B', unit_scale=True, ncols=80)

        def worker(start, end):
            sess = requests.Session()
            sess.headers.update(self.session.headers)
            sess.cookies.update(self.session.cookies)
            sess.headers['Range'] = f'bytes={start}-{end}'
            
            while True:
                try:
                    with sess.get(dlink, stream=True, timeout=20) as r:
                        r.raise_for_status()
                        with open(local_path, "r+b") as f:
                            f.seek(start)
                            for chunk in r.iter_content(65536):
                                f.write(chunk)
                                pbar.update(len(chunk))
                    break
                except: time.sleep(2)

        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(worker, s, e) for s,e in ranges]
            for f in as_completed(futures): pass
        
        pbar.close()

if __name__ == "__main__":
    # --- PASTE COOKIES HERE ---
    COOKIES = "ndus=xxxx"
    
    dl = TeraDownloader(COOKIES)
    
    url = input("Paste TeraBox URL: ").strip()
    
    print("[*] Initializing...")
    res = dl.get_metadata(url)
    
    if res:
        root_path, is_file = res
        
        all_files = []
        if is_file:
            print("[+] Single file detected.")
            all_files = dl.fetch_list(root_path, True)
        else:
            print(f"[*] Folder found at {root_path}. Crawling...")
            all_files = dl.crawl(root_path)
        
        if not all_files:
            print("\n[!] No files found. Check Cookies.")
        else:
            print(f"\n[+] Total Files: {len(all_files)}")
            for f in all_files:
                dl.download(f)
            
            print("\n[SUCCESS] All Done.")
