
import base64
import commonware
import datetime
import json
import polib
import pysvn
import requests
import traceback
from hashlib import md5

from django.conf import settings
from django.contrib import auth
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.urlresolvers import reverse
from django.core.validators import URLValidator
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect, Http404
from django.shortcuts import render
from django.utils.datastructures import MultiValueDictKeyError
from django_browserid import verify as browserid_verify
from django_browserid import get_audience
from pontoon.base.models import Locale, Project, Subpage, Entity, Translation
from session_csrf import anonymous_csrf_exempt

from mobility.decorators import mobile_template


log = commonware.log.getLogger('playdoh')


@mobile_template('{mobile/}home.html')
def home(request, error=None, locale=None, url=None, template=None):
    """Home view."""
    log.debug("Home view.")

    data = {
        'accept_language': request.META['HTTP_ACCEPT_LANGUAGE'].split(',')[0],
        'locales': Locale.objects.all(),
        'projects': Project.objects.filter(pk__in=Entity.objects.values('project'))
    }

    if error is not None:
        data['error'] = error

    if locale is not None:
        data['locale_code'] = locale

    if url is not None:
        data['project_url'] = url

    return render(request, template, data)

@mobile_template('{mobile/}translate.html')
def translate_site(request, locale, template=None):
    """Translate view: site."""
    log.debug("Translate view: site.")

    # Validate URL
    url = request.build_absolute_uri().split('/site/')[1]
    log.debug("URL: " + url)
    if url.find('://localhost') == -1 and url.find('gaiamobile.org') == -1:
        validate = URLValidator(verify_exists=True)
        try:
            validate(url)
        except ValidationError, e:
            log.debug(e)
            return home(request, "Oops, website could not be found.", locale, url)

    # Validate locale
    log.debug("Locale: " + locale)
    try:
        l = Locale.objects.get(code=locale)
    except Locale.DoesNotExist:
        return home(request, "Oops, locale is not supported.", locale, url)

    data = {
        'locale_code': locale,
        'project_url': url,
        'project': {},
        'projects': Project.objects.filter(pk__in=Entity.objects.values('project'))
    }

    if hasattr(settings, 'MICROSOFT_TRANSLATOR_API_KEY'):
        data['mt_apikey'] = settings.MICROSOFT_TRANSLATOR_API_KEY

    try:
        p = Project.objects.get(url=url)
        try:
            # Select project and a subpage
            s = Subpage.objects.get(url=url)
            page = s.name
        except Subpage.DoesNotExist:
            # Select project, subpage does not exist
            page = None
    except Project.DoesNotExist:
        try:
            # Select subpage and its project
            s = Subpage.objects.get(url=url)
            p = s.project
            page = s.name
        except Subpage.DoesNotExist:
            # Project not stored in the DB
            data['project']['locales'] = Locale.objects.all()
            return render(request, template, data)

    # Project stored in the DB, add more data
    if len(p.locales.filter(code=locale)) > 0:
        if page is None:
            return HttpResponseRedirect(reverse('pontoon.translate.project',
            kwargs={'locale': locale, 'project': p.name}))
        else:
            return HttpResponseRedirect(reverse('pontoon.translate.project.page',
            kwargs={'locale': locale, 'project': p.name, 'page': page}))

    else:
        return home(request, "Oops, locale is not supported for this website.", locale, url)

@mobile_template('{mobile/}translate.html')
def translate_project(request, locale, project, page=None, template=None):
    """Translate view: project."""
    log.debug("Translate view: project.")

    # Validate locale
    log.debug("Locale: " + locale)
    try:
        l = Locale.objects.get(code=locale)
    except Locale.DoesNotExist:
        return home(request, "Oops, locale is not supported.")

    # Validate project
    try:
        if project != 'gaiamobile':
            p = Project.objects.get(name=project, pk__in=Entity.objects.values('project'))
        else:
            p = Project.objects.get(name=project)
    except Project.DoesNotExist:
        return home(request, "Oops, project could not be found.", locale)

    data = {
        'locale_code': locale,
        'project_url': p.url,
        'project': p,
        'projects': Project.objects.filter(pk__in=Entity.objects.values('project'))
    }

    # Validate subpages
    pages = Subpage.objects.filter(project=p)
    if len(pages) > 0:
        if page is None:
            page = pages.filter(url=p.url)[0]
        else:
            try:
                s = pages.get(name=page)
                data['project_url'] = s.url
            except Subpage.DoesNotExist:
                return home(request, "Oops, subpage could not be found.", locale, p.url)
        data['pages'] = pages
        data['current_page'] = page

    # Validate project locales
    if len(p.locales.filter(code=locale)) == 0:
        return home(request, "Oops, locale is not supported for this website.", locale, data['project_url'])

    if hasattr(settings, 'MICROSOFT_TRANSLATOR_API_KEY'):
        data['mt_apikey'] = settings.MICROSOFT_TRANSLATOR_API_KEY

    return render(request, template, data)

def _request(type, project, resource, locale, username, password, payload=False):
    """
    Make request to Transifex server.

    Args:
        type: Request type
        project: Transifex project name
        resource: Transifex resource name
        locale: Locale code
        username: Transifex username
        password: Transifex password
        payload: Data to be sent to the server
    Returns:
        A server response or error message.
    """
    url = 'https://www.transifex.com/api/2/project/' + project + '/resource/' + resource + '/translation/' + locale + '/strings/'

    try:
        if type == 'get':
            r = requests.get(url + '?details', auth=(username, password), timeout=10)
        elif type == 'put':
            r = requests.put(url, auth=(username, password), timeout=10, 
                data=json.dumps(payload), headers={'content-type': 'application/json'})
        log.debug(r.status_code)
        if r.status_code == 401:
            return "authenticate"
        elif r.status_code != 200:
            return "error"
        return r
    # Network problem (DNS failure, refused connection, etc.)
    except requests.exceptions.ConnectionError, e:
        log.debug('ConnectionError: ' + str(e))
        return "error"
    # Invalid HTTP response
    except requests.exceptions.HTTPError, e:
        log.debug('HTTPError: ' + str(e))
        return "error"
    # A valid URL is required
    except requests.exceptions.URLRequired, e:
        log.debug('URLRequired: ' + str(e))
        return "error"
    # Request times out
    except requests.exceptions.Timeout, e:
        log.debug('Timeout: ' + str(e))
        return "error"
    # Request exceeds the number of maximum redirections
    except requests.exceptions.TooManyRedirects, e:
        log.debug('TooManyRedirects: ' + str(e))
        return "error"
    # Ambiguous exception occurres
    except requests.exceptions.RequestException, e:
        log.debug('RequestException: ' + str(e))
        return "error"
    except Exception:
        log.debug('Generic exception: ' + traceback.format_exc())
        return "error"

def get_translation(request, template=None):
    """Get entity translation of a specified project and locale."""
    log.debug("Get entity translation of a specified project and locale.")

    try:
        original = request.GET['original']
        url = request.GET['url']
        locale = request.GET['locale']
    except MultiValueDictKeyError:
        return HttpResponse("error")

    log.debug("Entity: " + original)
    log.debug("URL: " + url)
    log.debug("Locale: " + locale)

    p = Project.objects.filter(url=url)
    e = Entity.objects.filter(project=p, string=original)
    l = Locale.objects.get(code=locale)

    try:
        t = Translation.objects.get(entity=e, locale=l)
        log.debug("Translation: " + t.string)
        return HttpResponse(t.string)
    except Translation.DoesNotExist:
        log.debug("Translation does not exist.")
        return HttpResponse("error")

@login_required(redirect_field_name='', login_url='/404')
def save_translation(request, template=None):
    """Save entity translation to the specified project and locale."""
    log.debug("Save entity translation to the specified project and locale.")

    try:
        original = request.GET['original']
        translation = request.GET['translation']
        url = request.GET['url']
        locale = request.GET['locale']
    except MultiValueDictKeyError:
        return HttpResponse("error")

    log.debug("Entity: " + original)
    log.debug("Translation: " + translation)
    log.debug("URL: " + url)
    log.debug("Locale: " + locale)

    p = Project.objects.filter(url=url)
    e = Entity.objects.filter(project=p, string=original)
    l = Locale.objects.get(code=locale)

    try:
        """Update existing translation."""
        t = Translation.objects.get(entity=e[0], locale=l)
        t.string = translation
        t.save()

        if translation != '':
            log.debug("Translation updated.")
            return HttpResponse("updated")
        else:
            log.debug("Translation deleted.")
            return HttpResponse("deleted")

    except Translation.DoesNotExist:
        """Save new translation."""
        if translation != '':
            t = Translation(entity=e[0], locale=l, string=translation, 
                author=request.user.email, date=datetime.datetime.now())
            t.save()
            log.debug("Translation saved.")
            return HttpResponse("saved")
        else:
            log.debug("Translation not set.")
            return HttpResponse("not set")

def load_entities(request, template=None):
    """Load all project entities and translations."""
    log.debug("Load all project entities and translations.")

    try:
        callback = str(request.GET.get('callback', '')) # JSONP
        locale = request.GET['locale']
        url = request.GET['url']
    except MultiValueDictKeyError:
        return HttpResponse(callback + '("error");')

    log.debug("Locale: " + locale)
    log.debug("URL: " + url)

    """Query DB by project URL and locale."""
    try:
        l = Locale.objects.get(code=locale)
    except Locale.DoesNotExist:
        return HttpResponse(callback + '("error");')

    try:
        p = Project.objects.get(url=url)
    except Project.DoesNotExist:
        try:
            s = Subpage.objects.get(url=url)
            p = s.project
        except Subpage.DoesNotExist:
            log.debug("Project not found in the DB. Guessing entities.")
            return HttpResponse(callback + '("guess");')

    if len(p.locales.filter(code=locale)) > 0:
        log.debug("Load data from DB.")
        entities = Entity.objects.filter(project=p)

        data = []
        for e in entities:
            try:
                t = Translation.objects.get(entity=e, locale=l)
                translation = t.string
            except Translation.DoesNotExist:
                translation = ""

            obj = {
                "key": e.string,
                "comment": e.comment,
                "translation": translation
            }
            data.append(obj)

        return HttpResponse(callback + '(' + json.dumps(data, indent=4) + ');')
    else:
        return HttpResponse(callback + '("error");')

def _generate_po_content(data):
    """
    Generate PO content from data JSON.

    Args:
        data: A JSON string
    Returns:
        A string for generated PO content.
    """
    json_dict = json.loads(data)
    po = polib.POFile()
    current_time = datetime.datetime.now()
    metadata = json_dict.get('metadata', {})
    translations = json_dict.get('translations', {})

    # Add PO metadata
    po.metadata = {
        'Project-Id-Version': '1.0',
        'POT-Creation-Date': current_time,
        'PO-Revision-Date': current_time,
        'Last-Translator': '%s <%s>' % (json_dict['metadata'].get('username'),
            json_dict['metadata'].get('user_email')),
        'Language-Team': json_dict['metadata'].get('locale_language'),
        'MIME-Version': '1.0',
        'Content-Type': 'text/plain; charset=utf-8',
        'Content-Transfer-Encoding': '8bit'
    }

    # Add PO header
    po.header = (
            "%(project_title)s language file (%(locale_language)s)\n"
            "This is distributed under the same license as the website.\n"
            "%(username)s <%(user_email)s>, %(year)s\n" % {
                'project_title': metadata.get('project_title'),
                'locale_language': metadata.get('locale_language'),
                'username': metadata.get('username'),
                'user_email': metadata.get('user_email'),
                'year': current_time.year
             }
    )

    # Append PO entries
    for msgid in translations.keys():
        po_entry = polib.POEntry(
                msgid=msgid,
                msgstr=translations[msgid].get('msgstr', ''),
                occurrences = [(translations[msgid].get('occurrence', ''), '')]
        )
        if translations[msgid].get('fuzzy'):
            po_entry.flags.append('fuzzy')
        po.append(po_entry)
    return unicode(po).encode('utf-8')

@login_required(redirect_field_name='', login_url='/404')
def download(request, template=None):
    """Download translations in appropriate form."""
    log.debug("Download translations.")

    if request.method != 'POST':
        raise Http404

    try:
        type = request.POST['type']
        content = request.POST['content']
        locale = request.POST['locale']
    except MultiValueDictKeyError:
        raise Http404

    response = HttpResponse()
    if type == 'json':
        response['Content-Type'] = 'application/json'
    elif type == 'html':
        response['Content-Type'] = 'text/html'
    elif type == 'po':
        content = _generate_po_content(content)
        response['Content-Type'] = 'text/plain'

    response.content = content
    response['Content-Disposition'] = 'attachment; filename=' + locale +\
            '.' + type
    return response

@login_required(redirect_field_name='', login_url='/404')
def commit_to_svn(request, template=None):
    """Commit translations to SVN."""
    log.debug("Commit translations to SVN.")

    if request.method != 'POST':
        return HttpResponse("error")

    try:
        data = json.loads(request.POST['data'])
    except MultiValueDictKeyError:
        return HttpResponse("error")

    """Check if user authenticated to SVN."""
    profile = request.user.get_profile()
    username = data.get('auth', {}).get('username', profile.svn_username)
    password = data.get('auth', {}).get('password', base64.decodestring(profile.svn_password))
    if (len(username) == 0 or len(password) == 0):
        return HttpResponse("authenticate")

    locale = data['locale']
    content = data['content']

    try:
        p = Project.objects.get(url=data['url'])
    except Project.DoesNotExist:
        return HttpResponse("error")
    project = p.name

    client = pysvn.Client()
    client.set_default_username(username)
    client.set_default_password(password)

    f = open(settings.MEDIA_ROOT + '/svn/' + project + '/locale/' + locale +
        '/LC_MESSAGES/messages.po', 'w')
    f.write(_generate_po_content(content))
    f.close()

    """Save SVN username and password."""
    if 'auth' in data and 'remember' in data['auth'] and data['auth']['remember'] == 1:
        profile.svn_username = data['auth']['username']
        profile.svn_password = base64.encodestring(data['auth']['password'])
        profile.save()

    try:
        client.checkin([settings.MEDIA_ROOT + '/svn/' + project + '/locale/' + locale],
            'Pontoon: update ' + locale + ' localization of ' + project + '')
        return HttpResponse("200")
    except pysvn.ClientError, e:
        log.debug(str(e))
        return HttpResponse("error")

@login_required(redirect_field_name='', login_url='/404')
def save_to_transifex(request, template=None):
    """Save translations to Transifex."""
    log.debug("Save to Transifex.")

    if request.method != 'POST':
        return HttpResponse("error")

    try:
        data = json.loads(request.POST['data'])
    except MultiValueDictKeyError:
        return HttpResponse("error")

    """Check if user authenticated to Transifex."""
    profile = request.user.get_profile()
    username = data.get('auth', {}).get('username', profile.transifex_username)
    password = data.get('auth', {}).get('password', base64.decodestring(profile.transifex_password))
    if (len(username) == 0 or len(password) == 0):
        return HttpResponse("authenticate")

    """Make PUT request to Transifex API."""
    payload = []
    for entity in data.get('strings'):
        obj = {
            # Identify translation strings using hashes
            "source_entity_hash": md5(':'.join([entity['original'], '']).encode('utf-8')).hexdigest(),
            "translation": entity['translation']
        }
        payload.append(obj)
    log.debug(json.dumps(payload, indent=4))

    """Make PUT request to Transifex API."""
    try:
        p = Project.objects.get(url=data['url'])
    except Project.DoesNotExist:
        return HttpResponse("error")
    response = _request('put', p.transifex_project, p.transifex_resource, data['locale'], username, password, payload)

    """Save Transifex username and password."""
    if 'auth' in data and 'remember' in data['auth'] and data['auth']['remember'] == 1:
        profile.transifex_username = data['auth']['username']
        profile.transifex_password = base64.encodestring(data['auth']['password'])
        profile.save()

    try:
        return HttpResponse(response.status_code)
    except AttributeError:
        return HttpResponse(response)

@anonymous_csrf_exempt
def verify(request, template=None):
    """Verify BrowserID assertion, and return whether a user is registered."""
    log.debug("Verify BrowserID assertion.")

    if request.method != 'POST':
        raise Http404

    assertion = request.POST['assertion']
    if assertion is None:
        return HttpResponseBadRequest()

    verification = browserid_verify(assertion, get_audience(request))
    if not verification:
        return HttpResponseForbidden()

    response_data = {'registered': False, 'browserid': verification}
    user = auth.authenticate(assertion=assertion, audience=get_audience(request))
    if user is not None:
        auth.login(request, user)
        response_data = {'registered': True, 'browserid': verification,
            'manager': user.has_perm('base.can_manage')}

    return HttpResponse(json.dumps(response_data), mimetype='application/json')

def get_csrf(request, template=None):
    """Get CSRF token."""
    log.debug("Get CSRF token.")
    return HttpResponse(request.csrf_token)
