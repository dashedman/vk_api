# -*- coding: utf-8 -*-
"""
:authors: python273
:license: Apache License, Version 2.0, see LICENSE file

:copyright: (c) 2019 python273
"""
import logging
import re
import json
import time
from itertools import islice
from typing import Generator, Iterable

from bs4 import BeautifulSoup

import vk_api
from .audio_url_decoder import decode_audio_url
from .exceptions import AccessDenied, RetryAudioRequest
from .utils import set_cookies_from_list
from .upload import FilesOpener

RE_ALBUM_ID = re.compile(r'act=audio_playlist(-?\d+)_(\d+)')
RE_ACCESS_HASH = re.compile(r'access_hash=(\w+)')
RE_M3U8_TO_MP3 = re.compile(r'/[0-9a-f]+(/audios)?/([0-9a-f]+)/index.m3u8')

RE_USER_AUDIO_HASH = re.compile(r"AudioUtils.(un)?followOwner\(\d+, &#39;([^)]+)&#39;\)")

RPS_DELAY_RELOAD_AUDIO = 1.5
RPS_DELAY_LOAD_SECTION = 2.0

TRACKS_PER_USER_PAGE = 2000
TRACKS_PER_ALBUM_PAGE = 2000
ALBUMS_PER_USER_PAGE = 100


class VkAudio(object):
    """ Модуль для получения аудиозаписей без использования официального API.

    :param vk: Объект :class:`VkApi`
    """

    __slots__ = ('_vk', 'user_id', 'convert_m3u8_links', 'logger')

    DEFAULT_COOKIES = [
        {  # если не установлено, то первый запрос ломается
            'version': 0,
            'name': 'remixaudio_show_alert_today',
            'value': '0',
            'port': None,
            'port_specified': False,
            'domain': '.vk.com',
            'domain_specified': True,
            'domain_initial_dot': True,
            'path': '/',
            'path_specified': True,
            'secure': True,
            'expires': None,
            'discard': False,
            'comment': None,
            'comment_url': None,
            'rfc2109': False,
            'rest': {}
        }, {  # для аудио из постов
            'version': 0,
            'name': 'remixmdevice',
            'value': '1920/1080/2/!!-!!!!',
            'port': None,
            'port_specified': False,
            'domain': '.vk.com',
            'domain_specified': True,
            'domain_initial_dot': True,
            'path': '/',
            'path_specified': True,
            'secure': True,
            'expires': None,
            'discard': False,
            'comment': None,
            'comment_url': None,
            'rfc2109': False,
            'rest': {}
        }
    ]

    def __init__(self, vk: vk_api.VkApi, convert_m3u8_links=True):
        self.user_id = vk.method('users.get')[0]['id']
        self._vk = vk
        self.convert_m3u8_links = convert_m3u8_links

        set_cookies_from_list(self._vk.http.cookies, self.DEFAULT_COOKIES)

        self._vk.http.get('https://m.vk.com/')  # load cookies

        self.logger = logging.getLogger('vk_audio')

    def get_iter(self, owner_id=None, album_id=None, access_hash=None):
        """ Получить список аудиозаписей пользователя (по частям)

        :param owner_id: ID владельца (отрицательные значения для групп)
        :param album_id: ID альбома
        :param access_hash: ACCESS_HASH альбома
        """

        if owner_id is None:
            owner_id = self.user_id

        if album_id is not None:
            offset_diff = TRACKS_PER_ALBUM_PAGE
        else:
            offset_diff = TRACKS_PER_USER_PAGE

        offset = 0
        while True:
            response = self._vk.http.post(
                'https://m.vk.com/audio',
                data={
                    'act': 'load_section',
                    'owner_id': owner_id,
                    'playlist_id': album_id if album_id else -1,
                    'offset': offset,
                    'type': 'playlist',
                    'access_hash': access_hash,
                    'is_loading_all': 1
                },
                allow_redirects=False
            ).json()

            if not response['data'][0]:
                raise AccessDenied(
                    'You don\'t have permissions to browse {}\'s albums'.format(
                        owner_id
                    )
                )

            ids = scrap_ids(
                response['data'][0]['list']
            )
            if not ids:
                break

            tracks = scrap_tracks(
                ids,
                self.user_id,
                self._vk.http,
                convert_m3u8_links=self.convert_m3u8_links
            )

            for i in tracks:
                yield i

            if response['data'][0]['hasMore']:
                offset += offset_diff
            else:
                break

    def get(self, owner_id=None, album_id=None, access_hash=None):
        """ Получить список аудиозаписей пользователя

        :param owner_id: ID владельца (отрицательные значения для групп)
        :param album_id: ID альбома
        :param access_hash: ACCESS_HASH альбома
        """

        return list(self.get_iter(owner_id, album_id, access_hash))

    def get_albums_iter(self, owner_id=None):
        """ Получить список альбомов пользователя (по частям)

        :param owner_id: ID владельца (отрицательные значения для групп)
        """

        if owner_id is None:
            owner_id = self.user_id

        offset = 0

        while True:
            response = self._vk.http.get(
                'https://m.vk.com/audio?act=audio_playlists{}'.format(
                    owner_id
                ),
                params={
                    'offset': offset
                },
                allow_redirects=False
            )

            if not response.text:
                raise AccessDenied(
                    'You don\'t have permissions to browse {}\'s albums'.format(
                        owner_id
                    )
                )

            albums = scrap_albums(response.text)

            if not albums:
                break

            for i in albums:
                yield i

            offset += ALBUMS_PER_USER_PAGE

    def get_albums(self, owner_id=None):
        """ Получить список альбомов пользователя

        :param owner_id: ID владельца (отрицательные значения для групп)
        """

        return list(self.get_albums_iter(owner_id))

    def search_user(self, owner_id=None, q=''):
        """ Искать по аудиозаписям пользователя

        :param owner_id: ID владельца (отрицательные значения для групп)
        :param q: запрос
        """

        if owner_id is None:
            owner_id = self.user_id

        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'section',
                'claim': 0,
                'is_layer': 0,
                'owner_id': owner_id,
                'section': 'search',
                'q': q
            }
        )
        json_response = json.loads(response.text.replace('<!--', ''))

        if not json_response['payload'][1]:
            raise AccessDenied(
                'You don\'t have permissions to browse {}\'s audio'.format(
                    owner_id
                )
            )

        if json_response['payload'][1][1]['playlists']:

            ids = scrap_ids(
                json_response['payload'][1][1]['playlists'][0]['list']
            )

            tracks = scrap_tracks(
                ids,
                self.user_id,
                self._vk.http,
                convert_m3u8_links=self.convert_m3u8_links
            )

            return list(tracks)
        else:
            return []

    def edit_audio(self, audio_id: int, owner_id: int, hash: str, performer: str, title: str, text: str = "",
                   genre: int = 1001):
        """ Редактировать аудиозапись

        :param audio_id: ID аудиозаписи
        :param owner_id: ID владельца (отрицательные значения для групп)
        :param hash: хэш для редактирования аудиозаписи
        :param performer: название аудиозаписи
        :param title: заголовок аудиозаписи
        :param text: текст аудиозаписи
        :param genre: жанр аудиозаписи
        """
        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'edit_audio',
                'aid': audio_id,
                'oid': owner_id,
                'force_edit_hash': '',
                'hash': hash,
                'performer': performer,
                'text': text,
                'title': title,
                'genre': genre
            }
        )
        json_response = json.loads(response.text.replace('<!--', ''))
        return json_response["payload"][1][0]

    def upload_audio(self, audio: str, group_id: int = 0):
        """ Загрузка аудиозаписи

        :param group_id: ID группы, для юзера - 0
        """
        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'new_audio',
                'boxhash': base36encode(),
                'gid': group_id
            }
        )
        url = re.search("(https?:[^']*)", json.loads(response.text.replace('<!--', ''))["payload"][1][2]).group(0)
        with FilesOpener(audio, key_format='file') as f:
            uploader_response = self._vk.http.post(url, files=f).json()
        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'done_add',
                'go_uploader_response': json.dumps(uploader_response),
                'upldr': 1
            }
        )
        return json.loads(response.text.replace('<!--', ''))["payload"][1][0]

    def search(self, q, count=100, offset=0):
        """ Искать аудиозаписи

        :param q: запрос
        :param count: количество
        :param offset: смещение
        """

        return islice(self.search_iter(q, offset=offset), count)

    def search_iter(self, q, offset=0) -> Generator[dict, None, None]:
        """ Искать аудиозаписи (генератор)

        :param q: запрос
        :param offset: смещение
        """
        yield from self._iter_playlist_by_response({
            'al': 1,
            'act': 'section',
            'claim': 0,
            'is_layer': 0,
            'owner_id': self.user_id,
            'section': 'search',
            'q': q
        },
            targets=('Все треки',),
            offset=offset
        )

    def get_news_iter(self, offset=0):
        """ Искать популярные аудиозаписи  (генератор)

        :param offset: смещение
        """
        yield from self._iter_playlist_by_response({
            'al': 1,
            'act': 'section',
            'claim': 0,
            'is_layer': 0,
            'owner_id': self.user_id,
            'block': 'new_songs',
            'section': 'explore'
        },
            targets=('Новинки',),
            offset=offset
        )

    def get_updates_iter(self):
        """ Искать обновления друзей (генератор) """

        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'section',
                'claim': 0,
                'is_layer': 0,
                'owner_id': self.user_id,
                'section': 'updates'
            }
        )
        json_response = json.loads(response.text.replace('<!--', ''))

        while True:
            updates = [i['list'] for i in json_response['payload'][1][1]['playlists']]

            ids = scrap_ids(
                [i[0] for i in updates if i]
            )
            if not ids:
                break

            tracks = scrap_tracks(
                ids,
                self.user_id,
                convert_m3u8_links=self.convert_m3u8_links,
                http=self._vk.http
            )

            for track in tracks:
                yield track

            if len(updates) < 11:
                break

            response = self._vk.http.post(
                'https://vk.com/al_audio.php',
                data={
                    'al': 1,
                    'act': 'load_catalog_section',
                    'section_id': json_response['payload'][1][1]['sectionId'],
                    'start_from': json_response['payload'][1][1]['nextFrom']
                }
            )
            json_response = json.loads(response.text.replace('<!--', ''))

    def get_popular_iter(self, offset=0):
        """ Искать популярные аудиозаписи  (генератор)

        :param offset: смещение
        """

        response = self._vk.http.post(
            'https://vk.com/audio',
            data={
                'block': 'tracks_chart',
                'section': 'explore'
            }
        )
        json_response = json.loads(scrap_json(response.text))

        playlist = json_response['sectionData']['explore']['playlist']
        if not playlist:
            response = self._vk.http.post(
                'https://vk.com/al_audio.php',
                data={
                    'al': 1,
                    'act': 'load_catalog_section',
                    'section_id': json_response['sectionData']['explore']['sectionId'],
                }
            )
            json_response = json.loads(response.text.replace('<!--', ''))
            playlist = self.get_target_playlist_from_json(
                json_response,
                targets=('Чарт треков',)
            )
        ids = scrap_ids(playlist['list'])

        tracks = scrap_tracks(
            ids[offset:] if offset else ids,
            self.user_id,
            convert_m3u8_links=self.convert_m3u8_links,
            http=self._vk.http
        )

        for track in tracks:
            yield track

    def search_albums_iter(self, q) -> Generator[dict, None, None]:
        """ Искать альбомы (генератор)

        :param q: запрос
        """
        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'section',
                'claim': 0,
                'is_layer': 0,
                'owner_id': self.user_id,
                'section': 'search',
                'q': q
            }
        )
        json_response = json.loads(response.text.replace('<!--', ''))
        raw_html = json_response['payload'][1][0]
        raw_html = raw_html.split('CatalogSearchGlobalAlbumsHeader', 1)[1]
        try:
            raw_html = raw_html.split('/audio?section=recoms_block&type=', 1)[1]
        except IndexError:
            playlists = json_response['payload'][1][1]['playlists']
        else:
            block_id = raw_html.split('"', 1)[0]

            response = self._vk.http.post(
                'https://vk.com/al_audio.php',
                data={
                    'al': 1,
                    'act': 'load_catalog_section',
                    'section_id': block_id,
                }
            )
            json_response = json.loads(response.text.replace('<!--', ''))
            playlists = json_response['payload'][1][1]['playlists']
        albums = scrap_albums_from_al(playlists)

        for album in albums:
            yield album

    def _iter_playlist_by_response(self, data, targets: Iterable[str], offset: int = 0):
        """ Прокручивает плейлист,
        который возвращает в ответ al_audio (генератор)

        :param offset: параметры запроса в al_audio
        :param targets: Название целевого плейлиста для поиска в ответе
        :param offset: смещение
        """
        offset_left = 0

        def first_playlist():
            first_response = self._vk.http.post(
                'https://vk.com/al_audio.php',
                data=data
            )
            first_json = json.loads(first_response.text.replace('<!--', ''))
            return self.get_target_playlist_from_json(
                first_json, targets=targets)

        try:
            playlist = first_playlist()
        except RetryAudioRequest:
            playlist = first_playlist()

        while playlist:
            ids = scrap_ids(
                playlist['list']
            )
            if not ids:
                break

            if offset_left + len(ids) >= offset:
                if offset_left < offset:
                    ids = ids[offset - offset_left:]

                tracks = scrap_tracks(
                    ids,
                    self.user_id,
                    convert_m3u8_links=self.convert_m3u8_links,
                    http=self._vk.http
                )

                for track in tracks:
                    yield track

            offset_left += len(ids)

            if not playlist['hasMore']:
                return

            response = self._vk.http.post(
                'https://vk.com/al_audio.php',
                data={
                    'al': 1,
                    'act': 'load_catalog_section',
                    'section_id': playlist['id'],
                    'start_from': playlist['nextOffset']
                }
            )
            json_response = json.loads(response.text.replace('<!--', ''))
            # next part of playlist
            playlist = self.get_target_playlist_from_json(
                json_response, targets=targets
            )

    def get_audio_by_id(self, owner_id, audio_id):
        """ Получить аудиозапись по ID

        :param owner_id: ID владельца (отрицательные значения для групп)
        :param audio_id: ID аудио
        """
        response = self._vk.http.get(
            'https://m.vk.com/audio{}_{}'.format(owner_id, audio_id),
            allow_redirects=False
        )

        ids = scrap_ids_from_html(
            response.text,
            filter_root_el={'class': 'basisDefault'}
        )

        track = scrap_tracks(
            ids,
            self.user_id,
            http=self._vk.http,
            convert_m3u8_links=self.convert_m3u8_links
        )

        if track:
            return next(track)
        else:
            return []

    def get_post_audio(self, owner_id, post_id):
        """ Получить список аудиозаписей из поста пользователя или группы

        :param owner_id: ID владельца (отрицательные значения для групп)
        :param post_id: ID поста
        """
        response = self._vk.http.get(
            'https://m.vk.com/wall{}_{}'.format(owner_id, post_id)
        )

        ids = scrap_ids_from_html(
            response.text,
            filter_root_el={'class': 'audios_list'}
        )

        tracks = scrap_tracks(
            ids,
            self.user_id,
            http=self._vk.http,
            convert_m3u8_links=self.convert_m3u8_links
        )

        return tracks

    def follow_user(self, user_id):
        data = self._vk.http.get(f"https://vk.com/audios{user_id}")

        user_hash = RE_USER_AUDIO_HASH.search(data.text)
        if user_hash is None:
            raise AccessDenied(
                'You don\'t have permissions to browse {}\'s audio'.format(
                    user_id
                )
            )
        user_hash = user_hash.groups()[1]

        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'follow_owner',
                'owner_id': user_id,
                'hash': user_hash,
            }
        )
        json_response = json.loads(response.text.replace('<!--', ''))

        return json_response

    def unfollow_user(self, user_id):
        data = self._vk.http.get(f"https://vk.com/audios{user_id}")

        user_hash = RE_USER_AUDIO_HASH.search(data.text)
        if user_hash is None:
            raise AccessDenied(
                'You don\'t have permissions to browse {}\'s audio'.format(
                    user_id
                )
            )
        user_hash = user_hash.groups()[1]

        response = self._vk.http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'act': 'unfollow_owner',
                'owner_id': user_id,
                'hash': user_hash,
            }
        )
        json_response = json.loads(response.text.replace('<!--', ''))

        return json_response

    def get_target_playlist_from_json(self, json_data, targets: Iterable[str]) -> dict | None:
        if type(json_data['payload'][1][1]) == str:
            self._vk.auth()
            raise RetryAudioRequest(f'Catch error while unpacking json response: {json.dumps(json_data)}')

        try:
            playlists = json_data['payload'][1][1]['playlists']
        except Exception as e:
            self.logger.error('Catch error while unpacking json response: %s',
                              json.dumps(json_data))
            raise e
        target_playlist = next(
            (
                p for p in playlists
                if p['title'] in targets
            ),
            None
        )
        if target_playlist is None:
            exists_lists = [p['title'] for p in playlists if p['list']]
            self.logger.warning(
                'Cannot find playlist for %s! Exists lists: %s',
                list(targets), exists_lists
            )
        return target_playlist


def scrap_ids(audio_data):
    """ Парсинг списка хэшей аудиозаписей из json объекта """
    ids = []

    for track in audio_data:
        full_id = str(track[1]), str(track[0]), track[24]
        if all(full_id):
            ids.append(full_id)

    return ids


def scrap_json(html_page):
    """ Парсинг списка хэшей аудиозаписей новинок или популярных + nextFrom&sessionId """

    find_json_pattern = r"new AudioPage\(.*?(\{.*\})"
    fr = re.search(find_json_pattern, html_page).group(1)

    return fr


def scrap_ids_from_html(html, filter_root_el=None):
    """ Парсинг списка хэшей аудиозаписей из html страницы """

    if filter_root_el is None:
        filter_root_el = {'id': 'au_search_items'}

    soup = BeautifulSoup(html, 'html.parser')
    ids = []

    root_el = soup.find(**filter_root_el)

    if root_el is None:
        raise ValueError('Could not find root el for audio')

    playlist_snippets = soup.find_all('div', {'class': "audioPlaylistSnippet__list"})
    for playlist in playlist_snippets:
        playlist.decompose()

    for audio in root_el.find_all('div', {'class': 'audio_item'}):
        if 'audio_item_disabled' in audio['class']:
            continue

        data_audio = json.loads(audio['data-audio'])
        audio_hashes = data_audio[13].split("/")

        full_id = (
            str(data_audio[1]), str(data_audio[0]), audio_hashes[2], audio_hashes[5]
        )

        if all(full_id):
            ids.append(full_id)

    return ids


def scrap_tracks(ids, user_id, http, convert_m3u8_links=True):
    last_request = 0.0

    for ids_group in [ids[i:i + 10] for i in range(0, len(ids), 10)]:
        delay = RPS_DELAY_RELOAD_AUDIO - (time.time() - last_request)

        if delay > 0:
            time.sleep(delay)

        response = http.post(
            'https://vk.com/al_audio.php',
            data={
                'al': 1,
                'audio_ids': ','.join(['_'.join(i) for i in ids_group])
            },
            params={'act': 'reload_audios'},
        )
        result = json.loads(response.text.replace('<!--', ''))

        last_request = time.time()
        if result['payload']:
            data_audio = result['payload'][1][0]
            for audio in data_audio:
                artist = BeautifulSoup(audio[4], 'html.parser').text
                title = BeautifulSoup(audio[3].strip(), 'html.parser').text
                duration = audio[5]
                link = audio[2]

                if 'audio_api_unavailable' in link:
                    link = decode_audio_url(link, user_id)

                if convert_m3u8_links and 'm3u8' in link:
                    link = RE_M3U8_TO_MP3.sub(r'\1/\2.mp3', link)

                yield {
                    'id': audio[0],
                    'owner_id': audio[1],
                    'track_covers': audio[14].split(',') if audio[14] else [],
                    'url': link,

                    'artist': artist,
                    'title': title,
                    'duration': duration,
                }


def scrap_albums(html):
    """ Парсинг списка альбомов из html страницы """

    soup = BeautifulSoup(html, 'html.parser')
    albums = []

    for album in soup.find_all('div', {'class': 'audioPlaylistsPage__item'}):

        link = album.select_one('.audioPlaylistsPage__itemLink')['href']
        full_id = tuple(int(i) for i in RE_ALBUM_ID.search(link).groups())
        access_hash = RE_ACCESS_HASH.search(link)

        stats_text = album.select_one('.audioPlaylistsPage__stats').text

        # "1 011 прослушиваний"
        try:
            plays = int(stats_text.rsplit(' ', 1)[0].replace(' ', ''))
        except ValueError:
            plays = None

        albums.append({
            'id': full_id[1],
            'owner_id': full_id[0],
            'url': 'https://m.vk.com/audio?act=audio_playlist{}_{}'.format(
                *full_id
            ),
            'access_hash': access_hash.group(1) if access_hash else None,

            'title': album.select_one('.audioPlaylistsPage__title').text,
            'artist': album.select_one('.audioPlaylistsPage__author').text,
            'plays': plays
        })

    return albums


def scrap_albums_from_al(playlists):
    albums = []

    for raw_album in playlists:
        if raw_album['type'] != 'playlist':
            continue

        stats_text = raw_album['infoLine2']

        # "1<span class="num_delim"> </span>448<span class="num_delim"> </span>929 прослушиваний<span class="dvd"></span>9 аудиозаписей"
        try:
            plays = int(
                stats_text
                .rsplit('<span', 1)[0]
                .replace('<span class="num_delim"> </span>', '')
                .rsplit(' ', 1)[0]
            )  # 1448929
        except (ValueError, IndexError):
            plays = None

        full_id = (raw_album['ownerId'], raw_album['id'])
        albums.append({
            'id': full_id[1],
            'owner_id': full_id[0],
            'url': 'https://m.vk.com/audio?act=audio_playlist{}_{}'.format(
                *full_id
            ),
            'access_hash': raw_album['accessHash'],

            'title': raw_album['title'],
            'artist': raw_album['authorName'],
            'count': raw_album['totalCount'],
            'plays': plays
        })
    return albums


def base36encode():
    number = int(time.time() * 1000)
    alphabet = '0123456789abcdefghijklmnopqrstuvwxyz'
    base36 = ''

    while number != 0:
        number, i = divmod(number, len(alphabet))
        base36 = alphabet[i] + base36

    return base36
