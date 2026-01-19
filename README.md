# terabox-downloader
A simple python code for downloading terabox url

it can able to download 
file, folder and password protected link


by this i was able to get 
6 to 7 MB/s(free account) on usa and Japan server by using protonvpn


async_downloader.py have resume support 

lib required for async
```
pip install aiosqlite aiohttp aiofiles tqdm
```

# Don't forget to fill the cookies
```python
if __name__ == "__main__":
    # --- PASTE COOKIES HERE ---
    COOKIES = "ndus=xxxx"
```
