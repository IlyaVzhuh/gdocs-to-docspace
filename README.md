# gdocs-to-docspace

Перенос файлов из Google Docs, Sheets и Slides в self-hosted ONLYOFFICE DocSpace. Можно вставить ссылки на веб-странице, и файл появится в выбранной папке DocSpace от имени того, кто вошёл в систему. Есть и CLI для массового переноса.

## Как устроено

```
браузер ──https──► DocSpace (nginx) ──/gdocs-import/──► контейнер gdocs-import ──► docs.google.com (export docx/xlsx/pptx)
                        ▲                                       │
                        └──────── API DocSpace с cookie сессии ◄┘  (загрузка от имени пользователя)
```

- Страница открывается **на домене DocSpace**, поэтому браузер сам передаёт cookie сессии. Сервис не хранит ни паролей, ни ключей и работает с правами того, кто вошёл.
- Перед любым скачиванием проверяется сессия (`/api/2.0/people/@self`), чтобы сервис нельзя было использовать как анонимный прокси для загрузок.
- Защита от CSRF: принимается только `application/json` с `Origin` нашего домена, а тело запроса ограничено 8 КБ.
- Контейнер работает от `nobody`, с `read_only: true`, и на страницу установлен строгий CSP.

| Файл | Что делает |
| --- | --- |
| `gdocs2docspace.py` | CLI и библиотека: экспорт из Google по ссылке, загрузка в DocSpace (API-ключ или логин) |
| `server.py` | Веб-сервер на стандартной библиотеке Python (`http.server`), без зависимостей |
| `index.html` | Страница: вставить ссылки, выбрать папку, видеть статус каждого файла |
| `compose.yml` | Контейнер в сети DocSpace, с healthcheck |

## CLI

```bash
export DOCSPACE_URL=https://docspace.example.com
export DOCSPACE_API_KEY=...                       # или DOCSPACE_USER + DOCSPACE_PASSWORD
./gdocs2docspace.py -f urls.txt --folder 1234
./gdocs2docspace.py --cookies cookies.txt URL     # закрытые файлы Google: cookies.txt из браузера
```

## Веб-страница

```bash
docker compose up -d
```

В конфиг nginx перед DocSpace нужно добавить:

```nginx
location /gdocs-import/ {
    proxy_pass http://gdocs-import:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Обновление DocSpace может перезаписать конфиг nginx, поэтому после апгрейда этот блок стоит проверить.
