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

class LinkExpiredError(Exception):
    pass

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
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
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
            async with session.get(f"{self.base_url}/main", timeout=15) as r:
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
                m = re.search(r'%22([A-F0-9]{30,})%22', raw_token, re.IGNORECASE)
                if m: self.js_token = m.group(1)
                else: self.js_token = raw_token
            else:
                self.js_token = raw_token

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

    async def init_share(self, session, url):
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
            if not await self.verify_password(session, surl): return False
            async with session.get(info_url, params=params, headers=self.headers) as r:
                data = await r.json()

        if data.get('errno') != 0:
            print(f"[-] Init Error: {data}")
            return False

        self.auth = {
            'shareid': data['shareid'], 'uk': data['uk'], 'sign': data['sign'],
            'timestamp': data['timestamp'], 'sekey': urllib.parse.unquote(data['randsk']),
            'surl': surl
        }
        return True

    async def fetch_list(self, session, path, is_root=False):
        async with self.sem_api:
            ref_path = f"&path={urllib.parse.quote(path)}" if path != "/" else ""
            headers = self.headers.copy()
            headers['Referer'] = f'{self.base_url}/sharing/link?surl={self.auth["surl"]}{ref_path}'

            params = {
                'app_id': '250528', 'jsToken': self.js_token, 'page': 1, 'num': 2000,
                'order': 'name', 'desc': 1, 'shorturl': self.auth['surl']
            }
            params.update({k:v for k,v in self.auth.items() if k != 'surl'})

            if is_root: params['root'] = '1'
            else: params['dir'] = path

            attempt = 0
            while True:
                try:
                    async with session.get(f"{self.base_url}/share/list", params=params, headers=headers) as r:
                        data = await r.json()
                        errno = data.get('errno')
                        if errno == 0:
                            return data.get('list', [])

                        if errno in [4000020, 400141]:
                            backoff = min(2 ** attempt, 20)
                            if errno == 400141:
                                print(f"   [!] Risk Control on {path}. Waiting {backoff}s...")
                            else:
                                print("   [!] Token Expired. Refreshing...")
                            
                            await asyncio.sleep(backoff)
                            await self.get_token(session)
                            params['jsToken'] = self.js_token
                            attempt += 1
                            continue

                        print(f"   [-] API Fatal Error on {path}: {errno}")
                        return []

                except Exception as e:
                    print(f"   [-] Network Error on {path}: {e}")
                    await asyncio.sleep(2)
                    attempt += 1

    async def recursive_scan(self, session, path="/", is_root=True):
        if is_root: print("[*] Scanning Root...")
        else: print(f"   [+] Scanning: {path}")

        items = await self.fetch_list(session, path, is_root)
        files = []
        tasks = []
        
        for i in items:
            is_dir = str(i.get('isdir', '0')) == "1"
            if is_dir:
                tasks.append(self.recursive_scan(session, i['path'], is_root=False))
            else:
                if 'dlink' in i and i['dlink']:
                    files.append(i)

        if tasks:
            results = await asyncio.gather(*tasks)
            for sub_files in results:
                files.extend(sub_files)
                
        return files

    async def refresh_file_link(self, session, file_obj):
        print(f"\n   [↻] Link expired for: {file_obj['server_filename']}. Refreshing...")
        full_path = file_obj['path']
        parent_dir = os.path.dirname(full_path)
        if not parent_dir: parent_dir = "/"
        
        items = await self.fetch_list(session, parent_dir, is_root=(parent_dir == "/"))
        for item in items:
            if item['fs_id'] == file_obj['fs_id']:
                return item['dlink']
        
        print(f"   [!] Could not find file {file_obj['server_filename']} during refresh.")
        return None

    async def download_chunk(self, session, url, start, end, local_path, file_id, pbar):
        async with self.sem_download:
            headers = self.headers.copy()
            headers['Range'] = f'bytes={start}-{end}'
            
            attempts = 5
            backoff = 2
            
            while attempts > 0:
                try:
                    async with session.get(url, headers=headers, timeout=40) as resp:
                        if resp.status == 403:
                            raise LinkExpiredError("403 Forbidden")
                        if resp.status in [401, 410]:
                             raise Exception(f"HTTP {resp.status}")
                        resp.raise_for_status()
                        
                        async with aiofiles.open(local_path, mode='r+b') as f:
                            await f.seek(start)
                            async for chunk in resp.content.iter_chunked(65536):
                                await f.write(chunk)
                                pbar.update(len(chunk))
                        
                        await self.tracker.mark_chunk_done(file_id, start, end)
                        return True
                except LinkExpiredError:
                    raise
                except Exception:
                    attempts -= 1
                    if attempts == 0: return False
                    await asyncio.sleep(backoff + random.uniform(0, 1))
                    backoff *= 1.5
            return False

    async def download(self, session, file_obj):
        name = file_obj['server_filename']
        name = re.sub(r'[\\/*?:"<>|]', "", name)
        size = int(file_obj['size'])
        
        local_path = file_obj['path'].lstrip('/') if 'path' in file_obj else name
        local_dir = os.path.dirname(local_path)
        if local_dir and not os.path.exists(local_dir): os.makedirs(local_dir, exist_ok=True)

        file_id, is_in_db = await self.tracker.get_file_status(local_path)
        file_on_disk = os.path.exists(local_path)
        disk_size = os.path.getsize(local_path) if file_on_disk else 0

        if not is_in_db and file_on_disk and disk_size == size:
            print(f"[#] Skipping {name} (Complete)")
            return

        if not is_in_db:
            print(f"\n[+] Starting: {name} ({size/1024/1024:.2f} MB)")
            file_id = await self.tracker.register_file(local_path, size)
            with open(local_path, "wb") as f: f.truncate(size)
        else:
            if not file_on_disk or disk_size != size:
                print(f"[!] File changed/missing. Restarting {name}...")
                await self.tracker.cleanup_file(file_id)
                file_id = await self.tracker.register_file(local_path, size)
                with open(local_path, "wb") as f: f.truncate(size)
            else:
                print(f"\n[+] Resuming: {name}")

        chunk_size = CHUNK_SIZE_MB * 1024 * 1024
        all_ranges = []
        for i in range(0, size, chunk_size):
            end = min(i + chunk_size - 1, size - 1)
            all_ranges.append((i, end))

        max_link_refreshes = 3
        refresh_count = 0

        while refresh_count < max_link_refreshes:
            completed_chunks = await self.tracker.get_completed_chunks(file_id)
            completed_starts = set(r[0] for r in completed_chunks)
            needed_ranges = [r for r in all_ranges if r[0] not in completed_starts]
            
            if not needed_ranges:
                print("    [+] Verification Complete.")
                await self.tracker.cleanup_file(file_id)
                return

            bytes_to_download = sum((r[1] - r[0] + 1) for r in needed_ranges)
            initial = size - bytes_to_download
            pbar = tqdm(total=size, initial=initial, unit='B', unit_scale=True, ncols=80, desc=name[:15])

            try:
                tasks = [self.download_chunk(session, file_obj['dlink'], s, e, local_path, file_id, pbar) for s, e in needed_ranges]
                results = await asyncio.gather(*tasks)
                pbar.close()
                
                if all(results):
                    await self.tracker.cleanup_file(file_id)
                    print(f"[SUCCESS] {name}")
                    return
                else:
                    print(f"[!] {name} incomplete (Network/IO Error).")
                    return

            except LinkExpiredError:
                pbar.close()
                refresh_count += 1
                new_dlink = await self.refresh_file_link(session, file_obj)
                if new_dlink:
                    file_obj['dlink'] = new_dlink
                    continue 
                else:
                    print(f"[-] Failed to refresh link for {name}. Aborting.")
                    return
            except Exception as e:
                pbar.close()
                print(f"[-] Critical error downloading {name}: {e}")
                return

async def main():
    COOKIES = "ndus=xxxx"

    dl = AsyncTeraDownloader(COOKIES)
    await dl.tracker.init()
    
    url = input("Paste TeraBox URL: ").strip()
    
    async with aiohttp.ClientSession(
        cookies=dl.cookies, 
        connector=TCPConnector(limit=0, ssl=False),
        timeout=ClientTimeout(total=None, sock_connect=15, sock_read=60)
    ) as session:
        
        print("\n[Phase 1] Authenticating...")
        if await dl.init_share(session, url):
            
            print("\n[Phase 2] Deep Scanning (Recursive)...")
            all_files = await dl.recursive_scan(session, path="/", is_root=True)
            
            if not all_files:
                print("[!] No files found.")
            else:
                total_size = sum(int(f.get('size', 0)) for f in all_files)
                print(f"\n{'='*50}")
                print(f"Queue: {len(all_files)} files")
                print(f"Size:  {total_size / 1024 / 1024:.2f} MB")
                print(f"{'='*50}\n")
                
                print("[Phase 3] Downloading...")
                for f in all_files:
                    await dl.download(session, f)
                
                print("\n[SUCCESS] Operation Complete.")
        else:
            print("[!] Authentication Failed.")

if __name__ == "__main__":
    if os.name == 'nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try: asyncio.run(main())
    except KeyboardInterrupt: print("\n[!] Stopped.")
