import re

# Global regexps
re_token = re.compile('[0-9a-z]{64}')

# Key is implementation class.
# Value is a tuple of base url and a boolean indicating if the base url can be changed.
messaging_implementations = {
    'postgresqleu.util.messaging.bluesky.Bluesky': ('https://bsky.social', True),
    'postgresqleu.util.messaging.linkedin.Linkedin': ('https://api.linkedin.com', True),
    'postgresqleu.util.messaging.mastodon.Mastodon': ('https://mastodon.social', False),
    'postgresqleu.util.messaging.telegram.Telegram': ('https://api.telegram.org', True),
    'postgresqleu.util.messaging.twitter.Twitter': ('https://api.twitter.com', True),
}


def messaging_implementation_choices():
    return [(k, k.split('.')[-1], v[0], v[1]) for k, v in messaging_implementations.items()]


def get_messaging_class(classname):
    if classname not in messaging_implementations:
        raise Exception("Invalid messaging class")

    pieces = classname.split('.')
    modname = '.'.join(pieces[:-1])
    classname = pieces[-1]
    mod = __import__(modname, fromlist=[classname, ])
    return getattr(mod, classname)


def get_messaging_class_from_typename(typename):
    for k in messaging_implementations.keys():
        c = get_messaging_class(k)
        if c.typename.lower() == typename:
            return c
    return None


def get_messaging(provider):
    return get_messaging_class(provider.classname)(provider.id, provider.config)


class ProviderCache(object):
    def __init__(self):
        self.providers = {}

    def get(self, provider):
        if provider.id not in self.providers:
            self.providers[provider.id] = get_messaging_class(provider.classname)(provider.id, provider.config)
        return self.providers[provider.id]

    def get_by_id(self, providerid):
        from postgresqleu.confreg.models import MessagingProvider

        if providerid not in self.providers:
            provider = MessagingProvider.objects.get(pk=providerid)
            self.providers[providerid] = get_messaging_class(provider.classname)(providerid, provider.config)
        return self.providers[providerid]
