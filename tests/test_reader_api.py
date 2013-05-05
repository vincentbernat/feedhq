import json

from urllib import urlencode

from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.test import TestCase, Client
from mock import patch

from feedhq.feeds.models import Feed, Entry, UniqueFeed
from feedhq.reader.views import GoogleReaderXMLRenderer, item_id

from .factories import UserFactory, CategoryFactory, FeedFactory, EntryFactory
from . import responses


def clientlogin(token):
    """
    Authorization: header to pass to self.client.{get,post}() calls::

        self.client.post(url, data, **clientlogin(token))
    """
    return {'HTTP_AUTHORIZATION': 'GoogleLogin auth={0}'.format(token)}


class ApiClient(Client):
    def request(self, **request):
        response = super(ApiClient, self).request(**request)
        if response['Content-Type'] == 'application/json':
            response.json = json.loads(response.content)
        return response


class ApiTest(TestCase):
    client_class = ApiClient

    def setUp(self):  # noqa
        super(ApiTest, self).setUp()
        cache.clear()

    def auth_token(self, user):
        url = reverse('reader:login')
        response = self.client.post(url, {'Email': user.email,
                                          'Passwd': 'test'})
        for line in response.content.splitlines():
            key, value = line.split('=', 1)
            if key == 'Auth':
                return value

    def post_token(self, auth_token):
        url = reverse('reader:token')
        response = self.client.get(url, **clientlogin(auth_token))
        self.assertEqual(response.status_code, 200)
        return response.content


class AuthTest(ApiTest):
    def test_client_login_anon(self):
        url = reverse('reader:login')
        for response in (self.client.get(url), self.client.post(url)):
            self.assertContains(response, "Error=BadAuthentication",
                                status_code=403)

    def test_bad_auth_header(self):
        url = reverse('reader:tag_list')
        response = self.client.get(url, HTTP_AUTHORIZATION="GoogleLogin")
        self.assertEqual(response.status_code, 403)
        response = self.client.get(
            url, HTTP_AUTHORIZATION="GoogleLogin token=whatever")
        self.assertEqual(response.status_code, 403)

    def tests_client_login(self):
        url = reverse('reader:login')
        params = {
            'Email': 'test@example.com',
            'Passwd': 'brah',
        }
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 403)

        response = self.client.post(url, params)
        self.assertEqual(response.status_code, 403)

        user = UserFactory.create()
        params['Email'] = user.email
        response = self.client.get(url, params)
        self.assertEqual(response.status_code, 403)

        params['Passwd'] = 'test'
        response = self.client.get(url, params)
        self.assertContains(response, 'Auth=')

        response = self.client.post(url, params)
        self.assertContains(response, 'Auth=')

        # Usernames are also accepted
        params['Email'] = user.username
        response = self.client.post(url, params)
        self.assertContains(response, 'Auth=')

        # Case-insensitivity
        params['Email'] = user.username.upper()
        response = self.client.post(url, params)
        self.assertContains(response, 'Auth=')

        for line in response.content.splitlines():
            key, value = line.split('=', 1)
            self.assertEqual(len(value), 267)

    def test_post_token(self):
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:token')
        self.assertEqual(self.client.get(url).status_code, 403)

        response = self.client.get(url, **clientlogin("bad token"))
        self.assertEqual(response.status_code, 403)

        # First fetch puts the user in the cache
        with self.assertNumQueries(1):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 57)

        # Subsequent fetches use the cached user
        with self.assertNumQueries(0):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 57)

        cache.delete('reader_auth_token:{0}'.format(token))
        with self.assertNumQueries(1):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 200)

        user.auth_tokens.get().delete()  # deletes from cache as well
        with self.assertNumQueries(1):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 403)


class SerializerTest(ApiTest):
    def test_serializer(self):
        serializer = GoogleReaderXMLRenderer()
        self.assertEqual(serializer.render(None), '')

        serializer.render({'wat': {'of': 'dict'}})
        serializer.render({'stuff': ({'foo': 'bar'}, {'baz': 'blah'})})
        serializer.render({})
        serializer.render({'list': ('of', 'strings')})
        with self.assertRaises(AssertionError):
            serializer.render(12.5)


class ReaderApiTest(ApiTest):
    def test_user_info(self, get):
        url = reverse('reader:user_info')

        # Test bad authentication once and for all GET requests
        response = self.client.get(url)
        self.assertContains(response, "Error=BadAuthentication",
                            status_code=403)

        user = UserFactory.create()
        token = self.auth_token(user)
        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.json, {
            u"userName": user.username,
            u"userEmail": user.email,
            u"userId": str(user.pk),
            u"userProfileId": str(user.pk),
            u"isBloggerUser": False,
            u"signupTimeSec": int(user.date_joined.strftime("%s")),
            u"isMultiLoginEnabled": False,
        })

    def test_content_negociation(self, get):
        url = reverse('reader:user_info')
        user = UserFactory.create()
        token = self.auth_token(user)
        response = self.client.get(url, {'output': 'json'},
                                   **clientlogin(token))
        self.assertEqual(response['Content-Type'], 'application/json')

        response = self.client.get(url, {'output': 'xml'},
                                   **clientlogin(token))
        self.assertEqual(response['Content-Type'], 'application/xml')

    def test_subscriptions_list(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)

        url = reverse("reader:subscription_list")
        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.json, {"subscriptions": []})

        feed = FeedFactory.create(category__user=user, user=user)
        u = UniqueFeed.objects.get()
        u.link = 'http://example.com/foo'
        u.save(update_fields=['link'])
        EntryFactory.create(feed=feed, user=user)
        with self.assertNumQueries(2):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(len(response.json['subscriptions']), 1)
        self.assertEqual(response.json['subscriptions'][0]['categories'][0], {
            "id": "user/{0}/label/{1}".format(user.pk, feed.category.name),
            "label": feed.category.name,
        })

        FeedFactory.create(category__user=user, user=user)
        FeedFactory.create(category=feed.category, user=user)
        FeedFactory.create(category=feed.category, user=user)
        with self.assertNumQueries(2):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(len(response.json['subscriptions']), 4)

    def test_subscribed(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:subscribed')

        response = self.client.get(url, **clientlogin(token))
        self.assertContains(response, "Missing 's' parameter", status_code=400)

        response = self.client.get(url, {'s': 'foo/bar'}, **clientlogin(token))
        self.assertContains(response, "Unrecognized feed format",
                            status_code=400)

        feed_url = 'http://example.com/subscribed-feed'
        response = self.client.get(url, {'s': 'feed/{0}'.format(feed_url)},
                                   **clientlogin(token))
        self.assertContains(response, 'false')

        FeedFactory.create(url=feed_url, category__user=user, user=user)
        response = self.client.get(url, {'s': 'feed/{0}'.format(feed_url)},
                                   **clientlogin(token))
        self.assertContains(response, 'true')

    def test_edit_tag(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:edit_tag')
        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 405)

        response = self.client.post(url, **clientlogin(token))
        self.assertContains(response, "Missing 'T' POST token",
                            status_code=400)

        response = self.client.post(url, {'T': 'no'}, **clientlogin(token))
        self.assertContains(response, "Invalid POST token",
                            status_code=401)
        self.assertEqual(response['X-Reader-Google-Bad-Token'], 'true')

        token_url = reverse('reader:token')
        post_token = self.client.post(token_url, **clientlogin(token)).content

        data = {
            'T': post_token,
        }
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 'i' in request data",
                            status_code=400)

        data['i'] = 'tag:google.com,2005:reader/item/foobar'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Unrecognized item",
                            status_code=400)

        data['i'] = 'brah'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Unrecognized item",
                            status_code=400)

        entry = EntryFactory.create(user=user, feed__category__user=user)
        data['i'] = 'tag:google.com,2005:reader/item/{0}'.format(entry.hex_pk)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Specify a tag to add or remove",
                            status_code=400)

        data['r'] = 'unknown'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Bad tag format", status_code=400)

        # a and r at the same time
        data['a'] = 'user/-/state/com.google/starred'
        data['r'] = 'user/{0}/state/com.google/kept-unread'.format(user.pk)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK", status_code=200)
        e = user.entries.get()
        self.assertTrue(e.starred)
        self.assertTrue(e.read)

        del data['a']

        # Mark as read: remove "kept-unread" or add "read"
        self.assertFalse(entry.read)
        data['r'] = 'user/-/state/com.google/kept-unread'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        entry = Entry.objects.get()
        self.assertTrue(entry.read)

        entry.read = False
        entry.save(update_fields=['read'])
        del data['r']
        data['a'] = 'user/-/state/com.google/read'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        entry = Entry.objects.get()
        self.assertTrue(entry.read)

        # Mark as unread: add "kept-unread" or remove "read"
        data['a'] = 'user/-/state/com.google/kept-unread'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        entry = Entry.objects.get()
        self.assertFalse(entry.read)

        entry.read = True
        entry.save(update_fields=['read'])
        del data['a']
        data['r'] = 'user/-/state/com.google/read'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        entry = Entry.objects.get()
        self.assertFalse(entry.read)

        # Star / unstar, broadcast / unbroadcast
        for tag in ['starred', 'broadcast']:
            del data['r']
            data['a'] = 'user/-/state/com.google/{0}'.format(tag)
            response = self.client.post(url, data, **clientlogin(token))
            self.assertContains(response, "OK")
            entry = Entry.objects.get()
            self.assertTrue(getattr(entry, tag))

            data['r'] = data['a']
            del data['a']
            response = self.client.post(url, data, **clientlogin(token))
            self.assertContains(response, "OK")
            entry = Entry.objects.get()
            self.assertFalse(getattr(entry, tag))

        # Batch edition
        entry2 = EntryFactory.create(user=user, feed=entry.feed)
        self.assertEqual(user.entries.filter(broadcast=True).count(), 0)
        response = self.client.post(url, {
            'i': [entry.pk, entry2.pk],
            'a': 'user/-/state/com.google/broadcast',
            'T': post_token,
        }, **clientlogin(token))
        self.assertEqual(user.entries.filter(broadcast=True).count(), 2)

    def test_hex_item_ids(self, get):
        entry = Entry(pk=162170919393841362)
        self.assertEqual(entry.hex_pk, "024025978b5e50d2")
        entry.pk = -355401917359550817
        self.assertEqual(entry.hex_pk, "fb115bd6d34a8e9f")

        self.assertEqual(
            item_id("tag:google.com,2005:reader/item/fb115bd6d34a8e9f"),
            -355401917359550817
        )
        self.assertEqual(
            item_id("tag:google.com,2005:reader/item/024025978b5e50d2"),
            162170919393841362
        )

    def test_tag_list(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:tag_list')

        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(len(response.json['tags']), 2)

        CategoryFactory.create(user=user)
        with self.assertNumQueries(1):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(len(response.json['tags']), 3)

    def test_unread_count(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:unread_count')

        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.json, {'max': 1000, 'unreadcounts': []})

        feed = FeedFactory.create(category__user=user, user=user)
        for i in range(5):
            EntryFactory.create(feed=feed, read=False)
        feed2 = FeedFactory.create(category=feed.category, user=user)
        EntryFactory.create(feed=feed2, read=False)
        feed.update_unread_count()
        feed2.update_unread_count()

        with self.assertNumQueries(2):
            response = self.client.get(url, **clientlogin(token))

        # 3 elements: reading-list, label and feed
        self.assertEqual(len(response.json['unreadcounts']), 4)

        for count in response.json['unreadcounts']:
            if count['id'].endswith(feed2.url):
                self.assertEqual(count['count'], 1)
            elif count['id'].endswith((feed.category.name, 'reading-list')):
                self.assertEqual(count['count'], 6)
            else:
                self.assertEqual(count['count'], 5)

    def test_stream_content(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:stream_contents',
                      args=['user/-/state/com.google/reading-list'])

        # 2 are warmup queries, cached in following calls
        with self.assertNumQueries(4):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.json['author'], user.username)
        self.assertEqual(len(response.json['items']), 0)
        self.assertFalse('continuation' in response.json)

        # GET parameters validation
        response = self.client.get(url, {'ot': 'foo'}, **clientlogin(token))
        self.assertEqual(response.status_code, 400)
        response = self.client.get(url, {'ot': '13'}, **clientlogin(token))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(url, {'nt': 'foo'}, **clientlogin(token))
        self.assertEqual(response.status_code, 400)
        response = self.client.get(url, {'nt': '13'}, **clientlogin(token))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(url, {'r': 12, 'output': 'json'},
                                   **clientlogin(token))
        self.assertEqual(response.status_code, 200)
        response = self.client.get(url, {'r': 'o'}, **clientlogin(token))
        self.assertEqual(response.status_code, 200)

        response = self.client.get(url, {'n': 'foo'}, **clientlogin(token))
        self.assertEqual(response.status_code, 400)

        response = self.client.get(url, {'c': 'pageone'}, **clientlogin(token))
        response = self.client.get(url, {'c': 'a'}, **clientlogin(token))

        feed = FeedFactory.create(category__user=user, user=user)
        for i in range(15):
            EntryFactory.create(user=user, feed=feed, read=False)
        for i in range(4):
            EntryFactory.create(user=user, feed=feed, read=True)
        for i in range(10):
            EntryFactory.create(user=user, feed=feed, read=False, starred=True)
        EntryFactory.create(user=user, feed=feed, read=True, broadcast=True)

        # Warm up the uniques map cache
        with self.assertNumQueries(3):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.json['continuation'], 'page2')
        self.assertEqual(len(response.json['items']), 20)

        # ?xt= excludes stuff
        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': 'user/-/state/com.google/starred', 'n': 40},
                **clientlogin(token))
        self.assertEqual(len(response.json['items']), 20)

        # Multiple ?xt= is valid.
        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': [
                    'user/-/state/com.google/starred',
                    'user/-/state/com.google/broadcast-friends',
                    'user/-/state/com.google/lol',
                ], 'n': 40},
                **clientlogin(token))
        self.assertEqual(len(response.json['items']), 19)

        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': 'user/-/state/com.google/broadcast', 'n': 40},
                **clientlogin(token))
        self.assertEqual(len(response.json['items']), 29)

        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': 'user/-/state/com.google/kept-unread', 'n': 40},
                **clientlogin(token))
        self.assertEqual(len(response.json['items']), 5)

        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': 'user/-/state/com.google/read', 'n': 40},
                **clientlogin(token))
        self.assertEqual(len(response.json['items']), 25)

        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': 'feed/{0}'.format(feed.url)}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 0)

        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'xt': 'user/-/label/{0}'.format(feed.category.name)},
                **clientlogin(token))
        self.assertEqual(len(response.json['items']), 0)

        with self.assertNumQueries(2):
            response = self.client.get(url, {'c': 'page2'},
                                       **clientlogin(token))
        self.assertEqual(len(response.json['items']), 10)
        self.assertFalse('continuation' in response.json)
        self.assertTrue(response.json['self'][0]['href'].endswith(
            'reading-list?c=page2'))

        with self.assertNumQueries(2):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 30)
        self.assertFalse('continuation' in response.json)

        url = reverse('reader:stream_contents',
                      args=['user/-/state/com.google/starred'])
        with self.assertNumQueries(2):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 10)

        url = reverse('reader:stream_contents',
                      args=['user/-/label/{0}'.format(feed.category.name)])
        with self.assertNumQueries(2):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 30)

        url = reverse('reader:stream_contents',
                      args=['feed/{0}'.format(feed.url)])
        with self.assertNumQueries(4):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 30)

        url = reverse('reader:stream_contents',
                      args=['user/-/state/com.google/broadcast'])
        with self.assertNumQueries(2):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 1)

        url = reverse('reader:stream_contents',
                      args=['user/-/state/com.google/kept-unread'])
        with self.assertNumQueries(2):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 25)

        url = reverse('reader:stream_contents')  # defaults to reading-list
        with self.assertNumQueries(2):
            response = self.client.get(url, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 20)

        url = reverse('reader:stream_contents', args=['unknown'])
        response = self.client.get(url, **clientlogin(token))
        self.assertContains(response, "Unknown stream", status_code=400)

        response = self.client.get(
            reverse('reader:stream_contents',
                    args=['feed/http://inexisting.com/feed']),
            **clientlogin(token))
        self.assertEqual(response.status_code, 404)

        url = reverse('reader:stream_contents',
                      args=['user/-/state/com.google/like'])
        with self.assertNumQueries(2):
            response = self.client.get(url, {'n': 40}, **clientlogin(token))
        self.assertEqual(len(response.json['items']), 0)

    def test_stream_items_ids(self, get):
        get.return_value = responses(304)
        url = reverse("reader:stream_items_ids")
        user = UserFactory.create()
        token = self.auth_token(user)
        feed = FeedFactory.create(category__user=user, user=user)
        for i in range(5):
            EntryFactory.create(feed=feed, user=user, broadcast=True)
        for i in range(5):
            EntryFactory.create(feed=feed, user=user, starred=True, read=True)

        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 400)

        response = self.client.get(url, {'n': 'a'}, **clientlogin(token))
        self.assertEqual(response.status_code, 400)

        response = self.client.get(url, {'n': 10, 's': 'foo'},
                                   **clientlogin(token))
        self.assertEqual(response.status_code, 400)

        with self.assertNumQueries(2):
            response = self.client.post('{0}?{1}'.format(url, urlencode({
                'n': 5, 's': 'user/-/state/com.google/reading-list',
                'includeAllDirectStreamIds': 'true'})),
                **clientlogin(token))
        self.assertEqual(len(response.json['itemRefs']), 5)
        self.assertEqual(response.json['continuation'], 'page2')

        with self.assertNumQueries(2):
            response = self.client.post('{0}?{1}'.format(url, urlencode({
                'n': 5, 's': 'user/{0}/state/com.google/reading-list'.format(
                    user.pk),
                'includeAllDirectStreamIds': 'true'})),
                **clientlogin(token))
        self.assertEqual(len(response.json['itemRefs']), 5)
        self.assertEqual(response.json['continuation'], 'page2')

        with self.assertNumQueries(2):
            response = self.client.get(url, {
                'n': 5, 's': 'splice/user/-/state/com.google/reading-list',
                'includeAllDirectStreamIds': 'true'},
                **clientlogin(token))
        self.assertEqual(len(response.json['itemRefs']), 5)
        self.assertEqual(response.json['continuation'], 'page2')

        with self.assertNumQueries(2):
            response = self.client.get(url, {
                'n': 5,
                's': 'splice/user/{0}/state/com.google/reading-list'.format(
                    user.pk),
                'includeAllDirectStreamIds': 'true'},
                **clientlogin(token))
        self.assertEqual(len(response.json['itemRefs']), 5)
        self.assertEqual(response.json['continuation'], 'page2')

        with self.assertNumQueries(2):
            response = self.client.get(url, {
                'n': 5, 's': 'splice/user/-/state/com.google/reading-list',
                'c': 'page2', 'includeAllDirectStreamIds': 'true'},
                **clientlogin(token))
        self.assertEqual(len(response.json['itemRefs']), 5)
        self.assertFalse('continuation' in response.json)

        with self.assertNumQueries(2):
            response = self.client.get(url, {
                'n': 50, 's': (
                    'splice/user/-/state/com.google/broadcast|'
                    'user/{0}/state/com.google/read').format(user.pk),
                'includeAllDirectStreamIds': 'no'},
                **clientlogin(token))
        self.assertEqual(len(response.json['itemRefs']), 10)

    def test_stream_items_count(self, get):
        get.return_value = responses(304)
        url = reverse("reader:stream_items_count")
        user = UserFactory.create()
        token = self.auth_token(user)

        response = self.client.get(url, **clientlogin(token))
        self.assertEqual(response.status_code, 400)

        response = self.client.get(
            url, {'s': 'user/-/state/com.google/reading-list'},
            **clientlogin(token))
        self.assertEqual(response.content, '0')

        response = self.client.get(
            url, {'s': 'user/{0}/state/com.google/reading-list'.format(
                user.pk)},
            **clientlogin(token))
        self.assertEqual(response.content, '0')

        feed = FeedFactory.create(category__user=user, user=user)
        for i in range(6):
            EntryFactory.create(feed=feed, user=user, read=True)
        for i in range(4):
            EntryFactory.create(feed=feed, user=user)

        response = self.client.get(
            url, {'s': 'user/-/state/com.google/kept-unread'},
            **clientlogin(token))
        self.assertEqual(response.content, '4')

        response = self.client.get(
            url, {'s': 'user/-/state/com.google/read'},
            **clientlogin(token))
        self.assertEqual(response.content, '6')

        response = self.client.get(
            url, {'s': 'user/{0}/state/com.google/read'.format(user.pk)},
            **clientlogin(token))
        self.assertEqual(response.content, '6')

        response = self.client.get(
            url, {'s': 'user/-/state/com.google/kept-unread', 'a': 'true'},
            **clientlogin(token))
        self.assertTrue(response.content.startswith('4#'))

    def test_stream_items_contents(self, get):
        get.return_value = responses(304)
        url = reverse('reader:stream_items_contents')
        user = UserFactory.create()
        token = self.auth_token(user)

        response = self.client.get(url, **clientlogin(token))
        self.assertContains(response, "Required 'i' parameter",
                            status_code=400)

        response = self.client.get(url, {'i': 12}, **clientlogin(token))
        self.assertContains(response, "No items found", status_code=400)

        response = self.client.get(url, {'i': 12, 'output': 'atom'},
                                   **clientlogin(token))
        self.assertContains(response, "No items found", status_code=400)

        feed1 = FeedFactory.create(category__user=user, user=user)
        feed2 = FeedFactory.create(category__user=user, user=user)
        entry1 = EntryFactory.create(user=user, feed=feed1)
        entry2 = EntryFactory.create(user=user, feed=feed2)

        with self.assertNumQueries(2):
            response = self.client.get(url, {'i': [entry1.pk, entry2.pk]},
                                       **clientlogin(token))
            self.assertEqual(len(response.json['items']), 2)

        with self.assertNumQueries(1):
            response = self.client.get(url, {'i': [
                'tag:google.com,2005:reader/item/{0}'.format(entry1.hex_pk),
                entry2.pk]}, **clientlogin(token))
            self.assertEqual(len(response.json['items']), 2)

        with self.assertNumQueries(1):
            response = self.client.get(url, {'i': [
                'tag:google.com,2005:reader/item/{0}'.format(entry1.hex_pk),
                'tag:google.com,2005:reader/item/{0}'.format(entry2.hex_pk),
            ]}, **clientlogin(token))
            self.assertEqual(len(response.json['items']), 2)

        with self.assertNumQueries(1):
            response = self.client.get(url, {'i': [entry1.pk, entry2.pk],
                                             'output': 'atom'},
                                       **clientlogin(token))
            self.assertEqual(response.status_code, 200)

        with self.assertNumQueries(1):
            ids = ["tag:google.com,2005:reader/item/{0}".format(pk)
                   for pk in [entry1.hex_pk, entry2.hex_pk]]
            response = self.client.get(url, {'i': ids, 'output': 'atom'},
                                       **clientlogin(token))
            self.assertEqual(response.status_code, 200)

        feed3 = FeedFactory.create(category__user=user, user=user)
        entry3 = EntryFactory.create(user=user, feed=feed3)
        with self.assertNumQueries(2):
            response = self.client.get(
                url, {'i': [entry1.pk, entry2.pk, entry3.pk],
                      'output': 'atom-hifi'}, **clientlogin(token))
            self.assertEqual(response.status_code, 200)

    def test_mark_all_as_read(self, get):
        get.return_value = responses(304)
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:mark_all_as_read')

        token_url = reverse('reader:token')
        post_token = self.client.post(token_url, **clientlogin(token)).content

        feed = FeedFactory.create(category__user=user, user=user)
        for i in range(4):
            EntryFactory.create(feed=feed, user=user)
        EntryFactory.create(feed=feed, user=user, starred=True)
        EntryFactory.create(feed=feed, user=user, broadcast=True)

        feed2 = FeedFactory.create(category__user=user, user=user)
        entry = EntryFactory.create(feed=feed2, user=user)
        EntryFactory.create(feed=feed2, user=user, starred=True)
        EntryFactory.create(feed=feed2, user=user, starred=True)
        EntryFactory.create(feed=feed2, user=user, broadcast=True)

        data = {'T': post_token}
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 's' parameter", status_code=400)

        data['s'] = 'feed/{0}'.format(feed2.url)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=True).count(), 4)
        self.assertEqual(Feed.objects.get(pk=feed2.pk).unread_count, 0)

        entry.read = False
        entry.save(update_fields=['read'])
        feed2.update_unread_count()
        self.assertEqual(Feed.objects.get(pk=feed2.pk).unread_count, 1)

        data['s'] = 'user/-/label/{0}'.format(feed2.category.name)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=True).count(), 4)
        self.assertEqual(Feed.objects.get(pk=feed2.pk).unread_count, 0)

        data['s'] = 'user/{0}/label/{1}'.format(user.pk, feed2.category.name)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=True).count(), 4)
        self.assertEqual(Feed.objects.get(pk=feed2.pk).unread_count, 0)

        data['s'] = 'user/-/state/com.google/starred'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=True).count(), 5)
        self.assertEqual(Feed.objects.get(pk=feed.pk).unread_count, 5)
        self.assertEqual(Entry.objects.filter(starred=True,
                                              read=False).count(), 0)

        data['s'] = 'user/-/state/com.google/reading-list'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=False).count(), 0)
        for feed in Feed.objects.all():
            self.assertEqual(feed.unread_count, 0)

        data['s'] = 'user/-/state/com.google/read'  # yo dawg
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=False).count(), 0)

        data['s'] = 'user/{0}/state/com.google/read'.format(user.pk)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, 'OK')
        self.assertEqual(Entry.objects.filter(read=False).count(), 0)

    def test_stream_prefs(self, get):
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:stream_preference')
        response = self.client.get(url, **clientlogin(token))
        self.assertContains(response, "streamprefs")

    def test_preference_list(self, get):
        user = UserFactory.create()
        token = self.auth_token(user)
        url = reverse('reader:preference_list')
        response = self.client.get(url, **clientlogin(token))
        self.assertContains(response, "prefs")

    def test_edit_subscription(self, get):
        get.return_value = responses(304)

        user = UserFactory.create()
        token = self.auth_token(user)
        post_token = self.post_token(token)
        category = CategoryFactory.create(user=user)
        feed = FeedFactory.build(category=category, user=user)

        url = reverse('reader:subscription_edit')
        data = {'T': post_token}
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 'ac' parameter",
                            status_code=400)

        data['ac'] = 'subscribe'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 's' parameter", status_code=400)

        data['s'] = '{0}'.format(feed.url)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Unrecognized stream", status_code=400)

        data['s'] = 'feed/{0}'.format(feed.url)

        data['t'] = 'Testing stuff'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 'a' parameter", status_code=400)

        del data['t']
        data['a'] = 'user/-/label/foo'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 't' parameter", status_code=400)

        data['t'] = 'Testing stuff'
        data['a'] = 'userlabel/foo'
        get.return_value = responses(200, 'brutasse.atom')
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Unknown label", status_code=400)

        data['s'] = 'feed/foo bar'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Enter a valid URL", status_code=400)

        data['a'] = 'user/-/label/foo'
        data['s'] = 'feed/{0}'.format(feed.url)
        self.assertEqual(Feed.objects.count(), 0)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")

        self.assertEqual(Feed.objects.count(), 1)
        feed = Feed.objects.get()
        self.assertEqual(feed.name, 'Testing stuff')

        # Re-submit: existing
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "already subscribed", status_code=400)

        # Editing that
        data = {'T': post_token,
                'ac': 'edit',
                's': 'feed/{0}'.format(feed.url),
                'a': 'user/{0}/label/unknown'.format(user.pk)}
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "The label 'unknown' does not exist",
                            status_code=400)

        data['a'] = 'user/{0}/label/Other'.format(user.pk)
        cat = user.categories.create(name=u'Other', slug='unknown')
        response = self.client.post(url, data, **clientlogin(token))
        self.assertEqual(Feed.objects.get().category_id, cat.pk)

        data = {'T': post_token,
                'ac': 'edit',
                's': 'feed/{0}'.format(feed.url),
                't': 'Hahaha'}
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        self.assertEqual(Feed.objects.get().name, "Hahaha")

        # Unsubscribing
        data = {
            'T': post_token,
            'ac': 'unsubscribe',
            's': 'feed/{0}'.format(feed.url),
        }
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        self.assertEqual(Feed.objects.count(), 0)

        data['ac'] = 'test'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Unrecognized action", status_code=400)

    def test_quickadd_subscription(self, get):
        get.return_value = responses(304)

        user = UserFactory.create()
        token = self.auth_token(user)
        post_token = self.post_token(token)
        url = reverse('reader:subscription_quickadd')
        data = {
            'T': post_token,
        }
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing 'quickadd' parameter",
                            status_code=400)

        data['quickadd'] = 'foo bar'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Enter a valid URL",
                            status_code=400)

        feed = FeedFactory.build()
        data['quickadd'] = feed.url
        get.return_value = responses(200, 'brutasse.atom')
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "streamId")

        data['quickadd'] = 'feed/{0}'.format(feed.url)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "already subscribed", status_code=400)

    def test_disable_tag(self, get):
        user = UserFactory.create()
        token = self.auth_token(user)
        post_token = self.post_token(token)

        url = reverse('reader:disable_tag')
        data = {'T': post_token}
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "required 's'", status_code=400)

        data['t'] = 'test'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "does not exist", status_code=400)

        CategoryFactory.create(user=user, name=u'test')
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        self.assertEqual(user.categories.count(), 0)

        CategoryFactory.create(user=user, name=u'Other Cat')
        del data['t']
        data['s'] = 'user/{0}/label/Other Cat'.format(user.pk)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        self.assertEqual(user.categories.count(), 0)

    def test_rename_tag(self, get):
        user = UserFactory.create()
        token = self.auth_token(user)
        post_token = self.post_token(token)

        url = reverse('reader:rename_tag')

        data = {'T': post_token}
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing required 's'", status_code=400)

        data['t'] = 'test'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Missing required 'dest'",
                            status_code=400)

        data['dest'] = 'yolo'
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Invalid 'dest' parameter",
                            status_code=400)

        data['dest'] = 'user/{0}/label/yolo'.format(user.pk)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "Tag 'test' does not exist",
                            status_code=400)

        cat = CategoryFactory.create(user=user)
        data['t'] = cat.name
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        self.assertEqual(user.categories.get().name, 'yolo')

        data['s'] = 'user/{0}/label/yolo'.format(user.pk)
        del data['t']
        data['dest'] = 'user/{0}/label/Yo lo dawg'.format(user.pk)
        response = self.client.post(url, data, **clientlogin(token))
        self.assertContains(response, "OK")
        self.assertEqual(user.categories.get().name, 'Yo lo dawg')
ReaderApiTest = patch('requests.get')(ReaderApiTest)
