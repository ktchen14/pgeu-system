from django.shortcuts import render, get_object_or_404
from django.http import HttpResponseRedirect
from django.core.exceptions import PermissionDenied
from django.contrib.auth.decorators import login_required
from django.db import transaction, connection
from django.contrib import messages
from django.conf import settings
from django.template.defaultfilters import slugify
from django.utils import timezone

from io import StringIO
import difflib

from postgresqleu.confreg.models import ConferenceRegistration
from postgresqleu.confreg.util import render_conference_response
from postgresqleu.confreg.util import get_authenticated_conference, get_conference_or_404
from postgresqleu.confreg.util import reglog
from postgresqleu.confreg.util import send_conference_notification, send_conference_notification_template
from postgresqleu.confreg.util import send_conference_simple_mail
from postgresqleu.confreg.mail import attendee_email_form

from postgresqleu.util.db import exec_to_scalar, exec_to_list
from postgresqleu.util.request import get_int_or_error

from .models import Wikipage, WikipageHistory, WikipageSubscriber
from .forms import WikipageEditForm, WikipageAdminEditForm

from .models import Signup, AttendeeSignup
from .forms import SignupSubmitForm, SignupAdminEditForm, SignupSendmailForm
from .forms import SignupAdminEditSignupForm


def _check_wiki_permissions(request, page, readwrite=False):
    try:
        reg = ConferenceRegistration.objects.get(conference=page.conference, attendee=request.user, payconfirmedat__isnull=False, canceledat__isnull=True)
    except ConferenceRegistration.DoesNotExist:
        raise PermissionDenied("You must be registered for the conference in order to view this page")

    # Edit access implies both read and write
    if page.publicedit:
        return reg
    if page.editor_attendee.filter(id=reg.id).exists() or page.editor_regtype.filter(id=reg.regtype.id).exists():
        return reg
    if readwrite:
        raise PermissionDenied("Edit permission denied")
    # Now check read only
    if page.publicview:
        return reg
    if page.viewer_attendee.filter(id=reg.id).exists() or page.viewer_regtype.filter(id=reg.regtype.id).exists():
        return reg
    raise PermissionDenied("View permission denied")


def _check_signup_permissions(request, signup):
    try:
        reg = ConferenceRegistration.objects.get(conference=signup.conference, attendee=request.user, payconfirmedat__isnull=False, canceledat__isnull=True)
    except ConferenceRegistration.DoesNotExist:
        raise PermissionDenied("You must be registered for the conference in order to view this page")

    if signup.public:
        return reg
    if signup.attendees.filter(id=reg.id).exists() or signup.regtypes.filter(id=reg.regtype.id).exists():
        return reg
    raise PermissionDenied("Signup permission denied")


@login_required
def wikipage(request, confurl, wikiurl):
    conference = get_conference_or_404(confurl)
    page = get_object_or_404(Wikipage, conference=conference, url=wikiurl)
    reg = _check_wiki_permissions(request, page)

    is_subscribed = WikipageSubscriber.objects.filter(page=page, subscriber=reg).exists()

    # Ok, permissions to read. But does the user have permissions to
    # edit (so do we show the button?)
    editable = page.publicedit or page.editor_attendee.filter(id=reg.id).exists() or page.editor_regtype.filter(id=reg.regtype.id).exists()

    return render_conference_response(request, conference, 'wiki', 'confwiki/wikipage.html', {
        'page': page,
        'editable': editable,
        'is_subscribed': is_subscribed,
    })


@login_required
@transaction.atomic
def wikipage_subscribe(request, confurl, wikiurl):
    conference = get_conference_or_404(confurl)
    page = get_object_or_404(Wikipage, conference=conference, url=wikiurl)
    reg = _check_wiki_permissions(request, page)

    subs = WikipageSubscriber.objects.filter(page=page, subscriber=reg)
    if subs:
        subs.delete()
        messages.info(request, "{0} will no longer receive notifications for wiki page '{1}'.".format(reg.email, page.title))
    else:
        WikipageSubscriber(page=page, subscriber=reg).save()
        messages.info(request, "{0} will now receive notifications whenever wiki page '{1}' changes.".format(reg.email, page.title))

    return HttpResponseRedirect('../')


@login_required
def wikipage_history(request, confurl, wikiurl):
    conference = get_conference_or_404(confurl)
    page = get_object_or_404(Wikipage, conference=conference, url=wikiurl)
    reg = _check_wiki_permissions(request, page)
    if not page.history:
        raise PermissionDenied()

    fromid = toid = None

    if request.method == 'POST':
        # View a diff
        if not ('from' in request.POST and 'to' in request.POST):
            messages.warning(request, "Must specify both source and target version")
            return HttpResponseRedirect('.')

        page_from = get_object_or_404(WikipageHistory, page=page, pk=get_int_or_error(request.POST, 'from'))
        fromid = page_from.id
        if request.POST['to'] != '-1':
            page_to = get_object_or_404(WikipageHistory, page=page, pk=get_int_or_error(request.POST, 'to'))
            toid = page_to.id
        else:
            page_to = page
            toid = None

        diff = "\n".join(difflib.unified_diff(page_from.contents.split('\r\n'),
                                              page_to.contents.split('\r\n'),
                                              fromfile='{0}'.format(page_from.publishedat),
                                              tofile='{0}'.format(page_to.publishedat),
                                              lineterm='',
                                              ))
    else:
        diff = ''

    return render_conference_response(request, conference, 'wiki', 'confwiki/wikipage_history.html', {
        'page': page,
        'diff': diff,
        'fromid': fromid,
        'toid': toid,
    })


@login_required
@transaction.atomic
def wikipage_edit(request, confurl, wikiurl):
    conference = get_conference_or_404(confurl)
    page = get_object_or_404(Wikipage, conference=conference, url=wikiurl)
    reg = _check_wiki_permissions(request, page)

    baseform = True
    preview = ''
    diff = ''

    if request.method == 'POST':
        form = WikipageEditForm(instance=page, data=request.POST)
        if form.is_valid():
            # If nothing at all has changed, just redirect back
            if not form.instance.diff:
                return HttpResponseRedirect('../')

            diff = "\n".join(difflib.unified_diff(form.instance.diff['contents'][0].split('\r\n'),
                                                  form.instance.diff['contents'][1].split('\r\n'), fromfile='before', tofile='after', lineterm=''))

            # If we have changes, check if the preview has been viewed
            # or if it needs to be shown.
            if request.POST['submit'] == 'Commit changes':
                # Generate a history entry first, and then save. Copy the
                # author from the current page (not changed yet), but get
                # the contents from the previous instance. Then we can
                # change the author on the new record.
                WikipageHistory(page=page, author=page.author, contents=form.instance.diff['contents'][0], publishedat=page.publishedat).save()
                page.author = reg
                page.save()

                # Send notifications to admin and to any subscribers
                subject = '[{0}] Wiki page {1} changed'.format(conference.conferencename, page.title)
                body = "{0} has modified the page '{1}' with the following changes\n\n\n{2}\n\nPage: {3}/events/{4}/register/wiki/{5}/".format(
                    reg.fullname,
                    page.title,
                    diff,
                    settings.SITEBASE,
                    conference.urlname,
                    page.url,
                )
                send_conference_notification(
                    conference,
                    subject,
                    body
                )

                body += "\n\nYou are receiving this message because you are subscribed to changes to\nthis page. To stop receiving notifications, please click\n{0}/events/{1}/register/wiki/{2}/sub/\n\n".format(settings.SITEBASE, conference.urlname, page.url)
                for sub in WikipageSubscriber.objects.filter(page=page):
                    send_conference_simple_mail(conference,
                                                sub.subscriber.email,
                                                subject,
                                                body,
                                                receivername=sub.subscriber.fullname)

                return HttpResponseRedirect('../')
            elif request.POST['submit'] == 'Back to editing':
                # Fall through and render standard form
                pass
            else:
                # Else we clicked save
                baseform = False
                preview = form.cleaned_data['contents']
                form.fields['contents'].widget.attrs['class'] = 'hiddenfield'
    else:
        form = WikipageEditForm(instance=page)

    return render_conference_response(request, conference, 'wiki', 'confwiki/wikipage_edit.html', {
        'page': page,
        'form': form,
        'baseform': baseform,
        'preview': preview,
        'diff': diff,
    })


def admin(request, urlname):
    conference = get_authenticated_conference(request, urlname)

    pages = Wikipage.objects.filter(conference=conference)

    return render(request, 'confwiki/admin.html', {
        'conference': conference,
        'pages': pages,
        'helplink': 'wiki',
    })


@transaction.atomic
def admin_edit_page(request, urlname, pageid):
    conference = get_authenticated_conference(request, urlname)

    if pageid != 'new':
        page = get_object_or_404(Wikipage, conference=conference, pk=pageid)
    else:
        author = get_object_or_404(ConferenceRegistration, conference=conference, attendee=request.user)
        page = Wikipage(conference=conference, author=author)

    if request.method == 'POST':
        form = WikipageAdminEditForm(instance=page, data=request.POST)
        if form.is_valid():
            if pageid == 'new':
                form.save()
                send_conference_notification(
                    conference,
                    "Wiki page '{0}' created by {1}".format(form.cleaned_data['url'], request.user),
                    "Title: {0}\nAuthor: {1}\nPublic view: {2}\nPublic edit: {3}\nViewer types: {4}\nEditor types: {5}\nViewer attendees: {6}\nEditor attendees: {7}\n\n".format(
                        form.cleaned_data['title'],
                        form.cleaned_data['author'].fullname,
                        form.cleaned_data['publicview'],
                        form.cleaned_data['publicedit'],
                        ", ".join([r.regtype for r in form.cleaned_data['viewer_regtype']]),
                        ", ".join([r.regtype for r in form.cleaned_data['editor_regtype']]),
                        ", ".join([r.fullname for r in form.cleaned_data['viewer_attendee']]),
                        ", ".join([r.fullname for r in form.cleaned_data['editor_attendee']]),
                    ),
                )
            else:
                f = form.save(commit=False)
                form.save_m2m()
                s = StringIO()
                for k, v in list(f.diff.items()):
                    if type(v[0]) == list:
                        fr = ", ".join([str(o) for o in v[0]])
                    else:
                        fr = v[0]
                    if type(v[1]) == list:
                        to = ", ".join([str(o) for o in v[1]])
                    else:
                        to = v[1]
                    s.write("Changed {0} from {1} to {2}\n".format(k, fr, to))
                if s.tell() > 0:
                    s.write("\n\nPage admin: {}/events/admin/{}/wiki/{}/".format(settings.SITEBASE, conference.urlname, page.id))
                    # Something changed, so generate audit email
                    send_conference_notification(
                        conference,
                        "Wiki page '{0}' edited by {1}".format(form.cleaned_data['url'], request.user),
                        s.getvalue(),
                    )
                f.save()
            return HttpResponseRedirect('../')
    else:
        form = WikipageAdminEditForm(instance=page)

    return render(request, 'confwiki/admin_edit_form.html', {
        'conference': conference,
        'form': form,
        'page': page,
        'breadcrumbs': (('/events/admin/{0}/wiki/'.format(conference.urlname), 'Wiki'),),
        'helplink': 'wiki',
    })


@transaction.atomic
def admin_sendmail(request, urlname, pageid):
    conference = get_authenticated_conference(request, urlname)

    page = get_object_or_404(Wikipage, conference=conference, pk=pageid)

    if 'idlist' in request.GET or 'idlist' in request.POST:
        return attendee_email_form(
            request,
            conference,
            breadcrumbs=[
                ('../../', 'Wiki pages'),
                ('../', page.title),
            ],
        )

    if page.publicview:
        messages.error(request, "Cannot send wiki page email to public pages. Use regular attendee emails instead.")
        return HttpResponseRedirect("../")

    if page.viewer_regtype.exists():
        if page.viewer_attendee.exists():
            messages.warning(request, "Will not send wiki page emails based on regtype, only direct attendees. Use regular attendee emails to send to regtypes!")
        else:
            messages.error(request, "Cannot send wiki page email to pages with regtype permissions. Use regular attendee emails instead.")
            return HttpResponseRedirect("../")

    return HttpResponseRedirect("?idlist={}".format(",".join([str(r.id) for r in page.viewer_attendee.all()])))


@login_required
@transaction.atomic
def signup(request, urlname, signupid):
    conference = get_conference_or_404(urlname)
    signup = get_object_or_404(Signup, conference=conference, id=signupid)
    reg = _check_signup_permissions(request, signup)

    attendee_signup = AttendeeSignup.objects.filter(signup=signup, attendee=reg)
    if len(attendee_signup) == 1:
        attendee_signup = attendee_signup[0]
    else:
        attendee_signup = None

    if signup.visible and attendee_signup:
        # Include the results
        cursor = connection.cursor()
        cursor.execute("SELECT firstname || ' ' || lastname FROM confreg_conferenceregistration r INNER JOIN confwiki_attendeesignup a ON a.attendee_id=r.id WHERE a.signup_id=%(signup)s AND r.payconfirmedat IS NOT NULL AND r.canceledat IS NULL ORDER BY lastname, firstname", {
            'signup': signup.id,
        })
        current = [r[0] for r in cursor.fetchall()]
    else:
        current = None

    if signup.deadline and signup.deadline < timezone.now():
        # This one is closed
        return render_conference_response(request, conference, 'wiki', 'confwiki/signup.html', {
            'closed': True,
            'signup': signup,
            'attendee_signup': attendee_signup,
            'current': current,
        })

    # Signup is active, so build a form.
    if request.method == 'POST':
        form = SignupSubmitForm(signup, attendee_signup, data=request.POST)
        if form.is_valid():
            if form.cleaned_data['choice'] == '':
                # Remove instead!
                if attendee_signup:
                    if signup.notify_changes:
                        send_conference_notification_template(
                            conference,
                            'Signup response removed from {}'.format(signup.title),
                            'confwiki/mail/admin_notify_signup_delete.txt',
                            {
                                'conference': conference,
                                'signup': signup,
                                'attendeesignup': attendee_signup,
                            },
                        )
                    attendee_signup.delete()
                    reglog(reg, "Deleted response to signup {}".format(signup.id), request.user)
                    messages.info(request, "Your response has been deleted.")
                # If it did not exist, don't bother deleting it
            else:
                # Store an actual response
                if attendee_signup:
                    oldchoice = attendee_signup.choice
                    attendee_signup.choice = form.cleaned_data['choice']
                    if signup.notify_changes and oldchoice != attendee_signup.choice:
                        send_conference_notification_template(
                            conference,
                            'Signup response updated for {}'.format(signup.title),
                            'confwiki/mail/admin_notify_signup_modify.txt',
                            {
                                'conference': conference,
                                'signup': signup,
                                'attendeesignup': attendee_signup,
                                'oldchoice': oldchoice,
                            },
                        )
                    reglog(reg, "Updated response to signup {} from {} to {}".format(
                        signup.id,
                        oldchoice,
                        attendee_signup.choice,
                    ), request.user)
                else:
                    attendee_signup = AttendeeSignup(attendee=reg,
                                                     signup=signup,
                                                     choice=form.cleaned_data['choice'])
                    if signup.notify_changes:
                        send_conference_notification_template(
                            conference,
                            'Attendee signed up for {}'.format(signup.title),
                            'confwiki/mail/admin_notify_signup.txt',
                            {
                                'conference': conference,
                                'signup': signup,
                                'attendeesignup': attendee_signup,
                            },
                        )
                    reglog(reg, "Recorded response to signup {}".format(signup.id), request.user)
                attendee_signup.save()
                messages.info(request, "Your response has been stored. Thank you!")

            return HttpResponseRedirect('../../')
    else:
        form = SignupSubmitForm(signup, attendee_signup)

    return render_conference_response(request, conference, 'wiki', 'confwiki/signup.html', {
        'closed': False,
        'signup': signup,
        'attendee_signup': attendee_signup,
        'current': current,
        'form': form,
    })


def signup_admin(request, urlname):
    conference = get_authenticated_conference(request, urlname)

    signups = Signup.objects.filter(conference=conference)

    return render(request, 'confwiki/signup_admin.html', {
        'conference': conference,
        'signups': signups,
        'helplink': 'signups',
    })


@transaction.atomic
def signup_admin_edit(request, urlname, signupid):
    conference = get_authenticated_conference(request, urlname)

    if signupid != 'new':
        signup = get_object_or_404(Signup, conference=conference, pk=signupid)
        # There can be results, so calculate them. We want both a list and
        # a summary.
        results = {}
        cursor = connection.cursor()
        cursor.execute("WITH t AS (SELECT choice, count(*) AS num FROM confwiki_attendeesignup WHERE signup_id=%(signup)s GROUP BY choice) SELECT choice, num, CAST(num*100/sum(num) OVER () AS integer), CAST(num*100*4/sum(num) OVER () AS integer) FROM t ORDER BY 2 DESC", {
            'signup': signup.id,
        })
        sumresults = cursor.fetchall()
        results['summary'] = [dict(list(zip(['choice', 'num', 'percent', 'percentwidth'], r))) for r in sumresults]
        cursor.execute("SELECT s.id, firstname || ' ' || lastname,choice,saved FROM confreg_conferenceregistration r INNER JOIN confwiki_attendeesignup s ON r.id=s.attendee_id WHERE s.signup_id=%(signup)s ORDER BY saved", {
            'signup': signup.id,
        })
        results['details'] = [dict(list(zip(['id', 'name', 'choice', 'when'], r))) for r in cursor.fetchall()]
        if signup.optionvalues:
            optionstrings = signup.options.split(',')
            optionvalues = [int(x) for x in signup.optionvalues.split(',')]
            totalvalues = 0
            for choice, num, percent, width in sumresults:
                totalvalues += num * optionvalues[optionstrings.index(choice)]
            results['totalvalues'] = totalvalues

        # If we have a limited number of attendees, then we can generate
        # a list of pending users. We don't even try if it's set for public.
        if not signup.public:
            cursor.execute("SELECT firstname || ' ' || lastname FROM confreg_conferenceregistration r WHERE payconfirmedat IS NOT NULL AND canceledat IS NULL AND (regtype_id IN (SELECT registrationtype_id FROM confwiki_signup_regtypes srt WHERE srt.signup_id=%(signup)s) OR id IN (SELECT conferenceregistration_id FROM confwiki_signup_attendees WHERE signup_id=%(signup)s)) AND id NOT IN (SELECT attendee_id FROM confwiki_attendeesignup WHERE signup_id=%(signup)s) ORDER BY lastname, firstname", {
                'signup': signup.id,
            })
            results['awaiting'] = [dict(list(zip(['name', ], r))) for r in cursor.fetchall()]
    else:
        author = get_object_or_404(ConferenceRegistration, conference=conference, attendee=request.user)
        signup = Signup(conference=conference, author=author)
        results = None

    if request.method == 'POST':
        form = SignupAdminEditForm(instance=signup, data=request.POST)
        if form.is_valid():
            # We don't bother with diffs here as the only one who can
            # edit things are admins anyway.
            form.save()
            return HttpResponseRedirect('../')
    else:
        form = SignupAdminEditForm(instance=signup)

    return render(request, 'confwiki/signup_admin_edit_form.html', {
        'conference': conference,
        'form': form,
        'signup': signup,
        'results': results,
        'breadcrumbs': (('/events/admin/{0}/signups/'.format(conference.urlname), 'Signups'),),
        'helplink': 'signups',
    })


@transaction.atomic
def signup_admin_editsignup(request, urlname, signupid, id):
    conference = get_authenticated_conference(request, urlname)
    signup = get_object_or_404(Signup, conference=conference, pk=signupid)

    if id == 'new':
        attendeesignup = AttendeeSignup(signup=signup)
        reg = None
    else:
        attendeesignup = get_object_or_404(AttendeeSignup, signup=signup, pk=id)
        reg = attendeesignup.attendee

    if request.method == 'POST' and request.POST['submit'] == 'Delete':
        reglog(reg, "Admin removed response to signup {}".format(signup.id), request.user)
        attendeesignup.delete()
        return HttpResponseRedirect('../../')
    elif request.method == 'POST':
        form = SignupAdminEditSignupForm(signup, isnew=(id == 'new'), instance=attendeesignup, data=request.POST)
        if form.is_valid():
            if id == 'new':
                # Pick the registration that was selected in the form.
                reg = attendeesignup.attendee
            if (not signup.options) and (not form.cleaned_data['choice']):
                # Yes/no signup changed to no means we actually delete the
                # record completely.
                reglog(reg, "Admin removed response to signup {}".format(signup.id), request.user)
                attendeesignup.delete()
            else:
                form.save()
                reglog(reg, "Admin updated response to signup {}".format(signup.id), request.user)
            return HttpResponseRedirect('../../')
    else:
        form = SignupAdminEditSignupForm(signup, isnew=(id == 'new'), instance=attendeesignup)

    return render(request, 'confreg/admin_backend_form.html', {
        'basetemplate': 'confreg/confadmin_base.html',
        'conference': conference,
        'form': form,
        'what': 'attendee signup',
        'cancelurl': '../../',
        'allow_delete': (id != 'new'),
        'breadcrumbs': (
            ('/events/admin/{0}/signups/'.format(conference.urlname), 'Signups'),
            ('/events/admin/{0}/signups/{1}/'.format(conference.urlname, signup.id), signup.title),
        ),
        'helplink': 'signups',
    })


def _get_signup_email_query(signup, filters, optionstrings):
    params = {'confid': signup.conference.id, 'signup': signup.id}

    qq = "SELECT r.id AS regid, r.attendee_id AS user_id, r.firstname || ' ' || r.lastname AS fullname, r.email FROM confreg_conferenceregistration r WHERE payconfirmedat IS NOT NULL AND canceledat IS NULL AND conference_id=%(confid)s"
    if not signup.public:
        qq += " AND (regtype_id IN (SELECT registrationtype_id FROM confwiki_signup_regtypes srt WHERE srt.signup_id=%(signup)s) OR id IN (SELECT conferenceregistration_id FROM confwiki_signup_attendees WHERE signup_id=%(signup)s))"

    quals = []
    for r in filters:
        if r == 'responded':
            quals.append("EXISTS (SELECT 1 FROM confwiki_attendeesignup was WHERE was.signup_id=%(signup)s AND was.attendee_id=r.id)")
        elif r == 'noresp':
            quals.append("NOT EXISTS (SELECT 1 FROM confwiki_attendeesignup was WHERE was.signup_id=%(signup)s AND was.attendee_id=r.id)")

        elif r.startswith('r_'):
            optnum = int(r[2:])
            quals.append("EXISTS (SELECT 1 FROM confwiki_attendeesignup was WHERE was.signup_id=%(signup)s AND was.attendee_id=r.id AND choice=%(choice_{})s)".format(optnum))
            params['choice_{}'.format(optnum)] = optionstrings[optnum]
        elif r == 'all':
            quals.append("true")
        else:
            raise Exception("Unknown filter: {}".format(r))

    if quals:
        qq += " AND ({})".format(" OR ".join(quals))

    return qq, params


@transaction.atomic
def signup_admin_sendmail(request, urlname, signupid):
    conference = get_authenticated_conference(request, urlname)

    signup = get_object_or_404(Signup, conference=conference, pk=signupid)

    optionstrings = signup.options.split(',')

    if 'idlist' in request.GET or 'idlist' in request.POST:
        def _get_query(idlist):
            return _get_signup_email_query(signup, idlist, optionstrings)

        return attendee_email_form(
            request,
            conference,
            breadcrumbs=[
                ('../../', 'Signups'),
                ('../', signup.title),
            ],
            extracontext={'signup': signup},
            query=_get_query,
            strings=True,
        )

    additional_choices = [('r_{0}'.format(r), 'Recipients who responded {0}'.format(optionstrings[r])) for r in range(len(optionstrings))]

    if request.method == 'POST':
        form = SignupSendmailForm(conference, additional_choices, data=request.POST)
        if form.is_valid():
            # Calculate recipients and feed forward
            rr = form.cleaned_data['recipients']
            qq, params = _get_signup_email_query(signup, rr, optionstrings)
            recipients = exec_to_list(qq, params)
            if signup.public and 'all' in rr:
                messages.warning(request, "Since this is a public signup and you are sending to all attendees, you should probably consider using regular mail send instead of signup mail send, so it gets delivered to future attendees as well!")
            if len(recipients) == 0:
                form.add_error('recipients', 'No recipients match this criteria')
            else:
                return HttpResponseRedirect('?idlist={}'.format(",".join(rr)))
    else:
        form = SignupSendmailForm(conference, additional_choices)

    return render(request, 'confwiki/signup_sendmail_form.html', {
        'conference': conference,
        'form': form,
        'signup': signup,
        'breadcrumbs': (
            ('/events/admin/{0}/signups/'.format(conference.urlname), 'Signups'),
            ('/events/admin/{0}/signups/{1}/'.format(conference.urlname, signup.id), signup.title),
        ),
        'helplink': 'signups',
    })
