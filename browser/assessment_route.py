"""Allow the configured course site and its verified examination route."""
import re
from urllib.parse import urlsplit


EXAM_ORIGIN = 'https://examination.xuetangx.com'


def assessment_origin(url, site):
    def origin(parts):
        host = parts.hostname
        if not host or parts.username is not None or parts.password is not None or parts.scheme not in ('http', 'https'):
            return None
        host = f'[{host}]' if ':' in host else host.encode('idna').decode('ascii')
        port = parts.port
        default = 443 if parts.scheme == 'https' else 80
        return f'{parts.scheme}://{host}' + (f':{port}' if port and port != default else '')

    try:
        page, home = urlsplit(url), urlsplit(site)
        source, base = origin(page), origin(home)
    except (ValueError, UnicodeError):
        return None
    if source and source == base:
        return source
    if (source == EXAM_ORIGIN and home.scheme == 'https' and base
            and (home.hostname == 'yuketang.cn' or home.hostname.endswith('.yuketang.cn'))
            and re.fullmatch(r'/exam/[0-9]+/?', page.path)):
        return source
    return None
