# Brizy Missing Images Fix Guide

This document explains how to fix broken images on the project WordPress site when Brizy URLs return 404.

Project targets:
- Main domain: `https://www.vcawol.org.au`
- IP test site: `http://15.134.101.148`
- WordPress root on server: `~/html` (served from `/usr/share/nginx/html`)
- Uploads root: `/usr/share/nginx/html/wp-content/uploads`

## 1) Symptoms

You will usually see one of these errors:
- `There is no image with the uid "wp-..."`
- `The file ".../wp-content/uploads/... .webp" does not exist.`
- Many failed requests like:
  - `/?brizy_media=<id-or-uid>&brizy_crop=...`

## 2) Quick Health Check

Check if a page has broken uploads/Brizy images:

```bash
python3 - <<'PY'
import re,html,urllib.request,ssl
base='http://15.134.101.148'
page=f'{base}/news-page/'
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
text=html.unescape(urllib.request.urlopen(page,timeout=25,context=ctx).read().decode('utf-8','ignore'))
uploads=set(re.findall(r'(\/wp-content\/uploads\/[^"\'\s>]+\.(?:png|jpe?g|webp|gif|svg))', text, flags=re.I))
brizy=set(re.findall(r'(\/\?brizy_media=[^"\'\s>]+)', text, flags=re.I))
all_urls=set([base+u for u in uploads] + [base+u for u in brizy])
def head(url):
    req=urllib.request.Request(url,method='HEAD',headers={'User-Agent':'Mozilla/5.0'})
    with urllib.request.urlopen(req,timeout=15,context=ctx) as r:
        return r.status
bad=0
for url in all_urls:
    try:
        if head(url)>=400: bad+=1
    except Exception:
        bad+=1
print('candidates',len(all_urls),'bad',bad)
PY
```

If `bad > 0`, continue.

## 3) Identify Failing Brizy UIDs

Extract unique failing `brizy_media` values:

```bash
python3 - <<'PY'
import re,html,urllib.request,ssl
base='http://15.134.101.148'
page=f'{base}/news-page/'
ctx=ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
text=html.unescape(urllib.request.urlopen(page,timeout=25,context=ctx).read().decode('utf-8','ignore'))
brizy=set(re.findall(r'(\/\?brizy_media=[^"\'\s>]+)', text, flags=re.I))
uids=set()
for p in brizy:
    m=re.search(r'brizy_media=([^&]+)',p)
    if m: uids.add(m.group(1))
print('uids',len(uids))
for u in sorted(uids): print(u)
PY
```

## 4) Map UID/ID -> Expected Upload File

Use DB (`wp_postmeta`) to find what file Brizy expects:

```bash
.venv/bin/python - <<'PY'
import pymysql
ids=['7870','7878']  # replace with your failing values
conn=pymysql.connect(host='152.69.175.15',user='itt-admin',password='GCdGb!fNmk!!3dH',database='vcawol',connect_timeout=30)
with conn.cursor() as cur:
    for x in ids:
        if x.isdigit():
            post_id=int(x)
        else:
            cur.execute("SELECT p.ID FROM wp_posts p INNER JOIN wp_postmeta m ON (p.ID=m.post_id) WHERE p.post_type='attachment' AND m.meta_key='brizy_attachment_uid' AND m.meta_value=%s LIMIT 1",(x,))
            row=cur.fetchone()
            post_id=row[0] if row else None
        if not post_id:
            print(x,'-> no attachment')
            continue
        cur.execute("SELECT meta_value FROM wp_postmeta WHERE post_id=%s AND meta_key='_wp_attached_file' LIMIT 1",(post_id,))
        rel=cur.fetchone()[0]
        print(x,'->',post_id,rel)
conn.close()
PY
```

## 5) Restore Missing Files (Most Common Fix)

Most failures are missing `.webp` originals. Restore by:
1) download existing source from main domain (`.jpg`/`.png`)
2) generate `.webp` with `cwebp`
3) ensure perms are readable

Example (`h5.webp` missing, source is `h5.jpg`):

```bash
ssh vcawol 'set -e
B=/usr/share/nginx/html/wp-content/uploads
mkdir -p "$B/2025/03"
curl -fsSL "https://www.vcawol.org.au/wp-content/uploads/2025/03/h5.jpg" -o "$B/2025/03/h5.jpg"
cwebp -q 85 "$B/2025/03/h5.jpg" -o "$B/2025/03/h5.webp" >/dev/null
chmod 644 "$B/2025/03/h5.webp"
'
```

Tip to detect source extension quickly:

```bash
for ext in webp jpg jpeg png; do
  url="https://www.vcawol.org.au/wp-content/uploads/2025/03/h5.$ext"
  code=$(curl -sS -o /dev/null -w '%{http_code}' "$url")
  echo "$ext $code"
done
```

## 6) If Error Is `There is no image with the uid ...`

This means Brizy cannot map UID to attachment.

Fix flow:
1) import original file to media library (`wp media import`)
2) insert `brizy_attachment_uid` for that attachment

```bash
# import and capture attachment id
ssh vcawol 'cd ~/html && wp media import /usr/share/nginx/html/wp-content/uploads/2022/11/VOLUNTEER-min.png --porcelain'
```

Then add mapping in DB:

```bash
.venv/bin/python - <<'PY'
import pymysql
uid='wp-46778f15f05ec5a62e61e87e5197cf14.png'
post_id=7954  # use returned attachment id
conn=pymysql.connect(host='152.69.175.15',user='itt-admin',password='GCdGb!fNmk!!3dH',database='vcawol',autocommit=True)
with conn.cursor() as cur:
    cur.execute("INSERT INTO wp_postmeta (post_id, meta_key, meta_value) VALUES (%s,'brizy_attachment_uid',%s)",(post_id,uid))
conn.close()
PY
```

## 7) Re-Verify

Re-run the scan in section 2 on:
- `/news-page/`
- `/news-page/bpage/4/` to `/news-page/bpage/8/`

Expected result:
- `bad 0` for each page

## 8) Notes for This Project

- Focus path is `wp-content/uploads` only.
- Biggest impact is usually in `2022/10`, `2022/11`, and newer listing media.
- Brizy crop endpoint depends on the original upload path in `_wp_attached_file`; if that file is missing, all related crops fail.
