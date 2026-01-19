# terabox-downloader
A simple python code for downloading terabox url

it can able to download 
file, folder and password protected link

i have also added multi download, 
WORKERS var is the number of connection it create with single file to download 

by this i was able to get 
6 to 7 MB/s(free account) on usa and Japan server by using protonvpn

for terabox-downloader.py

```
pip install requests tqdm
```

async_downloader.py have resume support 

lib required for async
```
pip install aiosqlite aiohttp aiofiles tqdm
```
