import os
import re
import json
import asyncio
import urllib.parse
import aiohttp
import aiofiles
import aiosqlite
import random
from aiohttp import ClientTimeout, TCPConnector
from yarl import URL
from tqdm.asyncio import tqdm


DB_NAME = "download_history.db"
MAX_CONCURRENT_CHUNKS = 16
CHUNK_SIZE_MB = 1

class DownloadTracker:
    def __init__(self):
        self.db_path = DB_NAME

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('PRAGMA journal_mode=WAL;')
            await db.execute('PRAGMA synchronous=NORMAL;')
            
            await db.execute('CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE, size INTEGER)')
            await db.execute('CREATE TABLE IF NOT EXISTS chunks (file_id INTEGER, start_byte INTEGER, end_byte INTEGER, PRIMARY KEY (file_id, start_byte))')
            await db.commit()

    async def get_file_status(self, path):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT id FROM files WHERE path = ?', (path,)) as cursor:
                row = await cursor.fetchone()
                if row: return row[0], True
        return None, False

    async def register_file(self, path, size):
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('INSERT INTO files (path, size) VALUES (?, ?)', (path, size))
            await db.commit()
            return cursor.lastrowid

    async def get_completed_chunks(self, file_id):
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute('SELECT start_byte, end_byte FROM chunks WHERE file_id = ?', (file_id,)) as cursor:
                return await cursor.fetchall()

    async def mark_chunk_done(self, file_id, start, end):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('INSERT OR REPLACE INTO chunks (file_id, start_byte, end_byte) VALUES (?, ?, ?)', (file_id, start, end))
            await db.commit()

    async def cleanup_file(self, file_id):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('DELETE FROM chunks WHERE file_id = ?', (file_id,))
            await db.execute('DELETE FROM files WHERE id = ?', (file_id,))
            await db.commit()

class AsyncTeraDownloader:
    def __init__(self, cookie_string):
        self.base_url = "https://www.1024terabox.com"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/x-www-form-urlencoded',
            'Connection': 'keep-alive',
        }
        self.cookies = self._parse_cookies(cookie_string)
        self.js_token = ""
        self.auth = {}
        self.sem_download = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)
        self.sem_api = asyncio.Semaphore(5)
        self.tracker = DownloadTracker()

    def _parse_cookies(self, cookie_str):
        cookies = {}
        cookie_str = cookie_str.replace("Cookie: ", "")
        for c in cookie_str.split(';'):
            if '=' in c:
                k, v = c.strip().split('=', 1)
                cookies[k] = v
        return cookies

    async def get_token(self, session):
        try:
            async with session.get(f"{self.base_url}/main", timeout=10) as r:
                text = await r.text()
            
            tdata_match = re.search(r'<script>var templateData = (.*?);</script>', text)
            raw_token = ""
            if tdata_match:
                try:
                    data = json.loads(tdata_match.group(1))
                    raw_token = data.get('jsToken', '')
                except: pass
            
            if not raw_token:
                fn_match = re.search(r'fn\("%28%22(.*?)%22%29"\)', text)
                if fn_match: raw_token = fn_match.group(1)

            if "fn(" in raw_token or "%28" in raw_token:
                token_match = re.search(r'%22([A-F0-9]{30,})%22', raw_token, re.IGNORECASE)
                if token_match: self.js_token = token_match.group(1)
                else:
                    token_match = re.search(r'"([A-F0-9]{30,})"', raw_token, re.IGNORECASE)
                    if token_match: self.js_token = token_match.group(1)
            else:
                self.js_token = raw_token

            if self.js_token: print(f"[+] Token Cleaned: {self.js_token}")
        except Exception as e: print(f"[!] Token Error: {e}")

    async def verify_password(self, session, surl):
        loop = asyncio.get_running_loop()
        pwd = await loop.run_in_executor(None, input, ">> Enter Password: ")
        
        url = f"{self.base_url}/share/verify"
        params = {'app_id': '250528', 'jsToken': self.js_token, 'surl': surl}
        data = {'pwd': pwd.strip(), 'vcode': '', 'vcode_str': ''}
        
        self.headers['Referer'] = f'{self.base_url}/sharing/init?surl={surl}'
        
        async with session.post(url, params=params, data=data, headers=self.headers) as r:
            js = await r.json()
            if js.get('errno') == 0:
                print("[+] Password Accepted.")
                new_cookie = js.get('randsk')
                if new_cookie:
                    session.cookie_jar.update_cookies({'BOXCLND': new_cookie}, response_url=URL(self.base_url))
                return True
        return False

    async def get_metadata(self, session, url):
        key = url.strip().split()[0]
        if "/s/" in key: key = key.split("/s/")[-1].split("?")[0]
        if "surl=" in key: key = key.split("surl=")[-1].split("&")[0]
        surl = key[1:] if key.startswith('1') and len(key) > 20 else key
        
        if not self.js_token: await self.get_token(session)

        info_url = f"{self.base_url}/api/shorturlinfo"
        params = {'app_id': '250528', 'shorturl': key, 'root': '1', 'jsToken': self.js_token}
        
        async with session.get(info_url, params=params, headers=self.headers) as r:
            data = await r.json()

        if data.get('errno') in [-9]:
            print(f"[*] Link Locked. Verifying...")
            if not await self.verify_password(session, surl): return None
            async with session.get(info_url, params=params, headers=self.headers) as r:
                data = await r.json()

        if data.get('errno') != 0:
            print(f"[-] Metadata Error: {data}")
            return None

        self.auth = {
            'shareid': data['shareid'], 'uk': data['uk'], 'sign': data['sign'],
            'timestamp': data['timestamp'], 'sekey': urllib.parse.unquote(data['randsk']),
            'surl': surl
        }
        
        root = data['list'][0]
        is_file = (str(root.get('isdir', '1')) == "0")
        return root['path'], is_file

    async def fetch_list(self, session, path, is_single_file=False):
        async with self.sem_api:
            ref_path = f"&path={urllib.parse.quote(path)}" if path != "/" and not is_single_file else ""
            headers = self.headers.copy()
            headers['Referer'] = f'{self.base_url}/sharing/link?surl={self.auth["surl"]}{ref_path}'

            params = {
                'app_id': '250528', 'jsToken': self.js_token, 'page': 1, 'num': 1000,
                'order': 'name', 'desc': 1, 'shorturl': self.auth['surl']
            }
            params.update({k:v for k,v in self.auth.items() if k != 'surl'})

            if is_single_file: params['root'] = '1'
            else:
                params['dir'] = path
                if path == "/": params['root'] = '1'

            try:
                async with session.get(f"{self.base_url}/share/list", params=params, headers=headers) as r:
                    data = await r.json()
                    return data.get('list', [])
            except: return []

    async def crawl(self, session, path):
        items = await self.fetch_list(session, path, False)
        files = []
        tasks = []
        for i in items:
            if str(i.get('isdir', '0')) == "1":
                print(f"[*] Queuing Folder: {i['server_filename']}...")
                tasks.append(self.crawl(session, i['path']))
            else:
                files.append(i)
        if tasks:
            results = await asyncio.gather(*tasks)
            for sub_files in results: files.extend(sub_files)
        return files

    async def download_chunk(self, session, url, start, end, local_path, file_id, pbar):
        async with self.sem_download:
            headers = self.headers.copy()
            headers['Range'] = f'bytes={start}-{end}'
            
            attempts = 5
            backoff = 2
            
            while attempts > 0:
                try:
                    async with session.get(url, headers=headers, timeout=40) as resp:
                        if resp.status in [403, 429]: raise Exception(f"HTTP {resp.status}")
                        resp.raise_for_status()
                        
                        async with aiofiles.open(local_path, mode='r+b') as f:
                            await f.seek(start)
                            async for chunk in resp.content.iter_chunked(65536):
                                await f.write(chunk)
                                pbar.update(len(chunk))

                        await self.tracker.mark_chunk_done(file_id, start, end)
                        return True
                except Exception:
                    attempts -= 1
                    if attempts == 0: return False
                    await asyncio.sleep(backoff + random.uniform(0, 1))
                    backoff *= 1.5
            return False

    async def download(self, session, file_obj):
        name = file_obj['server_filename']
        size = int(file_obj['size'])
        dlink = file_obj['dlink']
        
        local_path = file_obj['path'].lstrip('/') if 'path' in file_obj else name
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.exists(local_dir): os.makedirs(local_dir, exist_ok=True)

        # 1. Check DB
        file_id, is_in_db = await self.tracker.get_file_status(local_path)
        file_on_disk = os.path.exists(local_path)
        disk_size = os.path.getsize(local_path) if file_on_disk else 0

        # 2. Skip if physically done but not in DB (Already downloaded)
        if not is_in_db and file_on_disk and disk_size == size:
            print(f"[#] Skipping {name} (Exists on disk)")
            return

        # 3. Setup
        if not is_in_db:
            print(f"\n[+] Starting: {name} ({size/1024/1024:.2f} MB)")
            file_id = await self.tracker.register_file(local_path, size)
            with open(local_path, "wb") as f: f.truncate(size)
        else:
            if not file_on_disk or disk_size != size:
                print(f"[!] Corruption. Restarting {name}...")
                await self.tracker.cleanup_file(file_id)
                file_id = await self.tracker.register_file(local_path, size)
                with open(local_path, "wb") as f: f.truncate(size)
            else:
                print(f"\n[+] Resuming: {name}")

        # 4. Range Calculation
        chunk_size = CHUNK_SIZE_MB * 1024 * 1024 
        all_ranges = []
        for i in range(0, size, chunk_size):
            end = min(i + chunk_size - 1, size - 1)
            all_ranges.append((i, end))

        completed_chunks = await self.tracker.get_completed_chunks(file_id)
        completed_starts = set(r[0] for r in completed_chunks)
        needed_ranges = [r for r in all_ranges if r[0] not in completed_starts]
        
        if not needed_ranges:
            print("[+] DB Check: Complete.")
            await self.tracker.cleanup_file(file_id)
            return

        # 5. Accurate Progress Report
        bytes_to_download = sum((r[1] - r[0] + 1) for r in needed_ranges)
        initial_progress = size - bytes_to_download

        print(f"    Saved Status: {initial_progress/1024/1024:.2f} MB / {size/1024/1024:.2f} MB ({100*(initial_progress/size):.1f}%)")

        pbar = tqdm(total=size, initial=initial_progress, 
                    unit='B', unit_scale=True, ncols=80, desc=name[:15])

        tasks = [self.download_chunk(session, dlink, s, e, local_path, file_id, pbar) for s, e in needed_ranges]
        
        # Capture results to check completion
        results = await asyncio.gather(*tasks)
        pbar.close()
        
        if all(results):
            await self.tracker.cleanup_file(file_id)
            print(f"[SUCCESS] {name} saved.")
        else:
            print(f"[!] {name} interrupted. Progress saved.")

async def main():
    # --- PASTE COOKIES HERE ---
    COOKIES = "ndus=xxxx"
    
    dl = AsyncTeraDownloader(COOKIES)
    await dl.tracker.init()
    
    url = input("Paste TeraBox URL: ").strip()
    
    async with aiohttp.ClientSession(
        cookies=dl.cookies, 
        connector=TCPConnector(limit=0, ssl=False),
        timeout=ClientTimeout(total=None, sock_connect=15, sock_read=60)
    ) as session:
        
        print("[*] Initializing...")
        res = await dl.get_metadata(session, url)
        
        if res:
            root_path, is_file = res
            if is_file:
                all_files = await dl.fetch_list(session, root_path, True)
            else:
                all_files = await dl.crawl(session, root_path)
            
            print(f"\n[+] Total Files: {len(all_files)}")
            for f in all_files:
                await dl.download(session, f)

if __name__ == "__main__":
    if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n[!] Stopped.")
