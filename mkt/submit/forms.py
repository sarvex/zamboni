import datetime
import os
from collections import defaultdict

from django import forms
from django.conf import settings

import basket
import happyforms
import waffle
from tower import ugettext as _, ugettext_lazy as _lazy

import amo
from amo.utils import slug_validator
from mkt.comm.utils import create_comm_note
from mkt.constants import APP_FEATURES, comm, FREE_PLATFORMS, PAID_PLATFORMS
from mkt.developers.forms import verify_app_domain
from mkt.files.models import FileUpload
from mkt.files.utils import parse_addon
from mkt.reviewers.models import RereviewQueue
from mkt.translations.fields import TransField
from mkt.translations.forms import TranslationFormMixin
from mkt.translations.widgets import TransInput, TransTextarea
from mkt.users.models import UserNotification
from mkt.users.notifications import app_surveys
from mkt.webapps.models import Addon, AppFeatures, BlacklistedSlug, Webapp


def mark_for_rereview(addon, added_devices, removed_devices):
    msg = _(u'Device(s) changed: {0}').format(', '.join(
        [_(u'Added {0}').format(unicode(amo.DEVICE_TYPES[d].name))
         for d in added_devices] +
        [_(u'Removed {0}').format(unicode(amo.DEVICE_TYPES[d].name))
         for d in removed_devices]))
    RereviewQueue.flag(addon, amo.LOG.REREVIEW_DEVICES_ADDED, msg)


def mark_for_rereview_features_change(addon, added_features, removed_features):
    # L10n: {0} is the list of requirements changes.
    msg = _(u'Requirements changed: {0}').format(', '.join(
        [_(u'Added {0}').format(f) for f in added_features] +
        [_(u'Removed {0}').format(f) for f in removed_features]))
    RereviewQueue.flag(addon, amo.LOG.REREVIEW_FEATURES_CHANGED, msg)


class DeviceTypeForm(happyforms.Form):
    ERRORS = {
        'both': _lazy(u'Cannot be free and paid.'),
        'none': _lazy(u'Please select a device.'),
        'packaged': _lazy(u'Packaged apps are not yet supported for those '
                          u'platforms.'),
    }

    free_platforms = forms.MultipleChoiceField(
        choices=FREE_PLATFORMS(), required=False)
    paid_platforms = forms.MultipleChoiceField(
        choices=PAID_PLATFORMS(), required=False)

    def save(self, addon, is_paid):
        data = self.cleaned_data[
            'paid_platforms' if is_paid else 'free_platforms']
        submitted_data = self.get_devices(t.split('-', 1)[1] for t in data)

        new_types = set(dev.id for dev in submitted_data)
        old_types = set(amo.DEVICE_TYPES[x.id].id for x in addon.device_types)

        added_devices = new_types - old_types
        removed_devices = old_types - new_types

        for d in added_devices:
            addon.addondevicetype_set.create(device_type=d)
        for d in removed_devices:
            addon.addondevicetype_set.filter(device_type=d).delete()

        # Send app to re-review queue if public and new devices are added.
        if added_devices and addon.status in amo.WEBAPPS_APPROVED_STATUSES:
            mark_for_rereview(addon, added_devices, removed_devices)

    def _add_error(self, msg):
        self._errors['free_platforms'] = self._errors['paid_platforms'] = (
            self.ERRORS[msg])

    def _get_combined(self):
        devices = (self.cleaned_data.get('free_platforms', []) +
                   self.cleaned_data.get('paid_platforms', []))
        return set(d.split('-', 1)[1] for d in devices)

    def _set_packaged_errors(self):
        """Add packaged-app submission errors for incompatible platforms."""
        devices = self._get_combined()
        bad_android = (
            not waffle.flag_is_active(self.request, 'android-packaged') and
            ('android-mobile' in devices or 'android-tablet' in devices)
        )
        bad_desktop = (
            not waffle.flag_is_active(self.request, 'desktop-packaged') and
            'desktop' in devices
        )
        if bad_android or bad_desktop:
            self._errors['free_platforms'] = self._errors['paid_platforms'] = (
                self.ERRORS['packaged'])

    def clean(self):
        data = self.cleaned_data
        paid = data.get('paid_platforms', [])
        free = data.get('free_platforms', [])

        # Check that they didn't select both.
        if free and paid:
            self._add_error('both')
            return data

        # Check that they selected one.
        if not free and not paid:
            self._add_error('none')
            return data

        return super(DeviceTypeForm, self).clean()

    def get_devices(self, source=None):
        """Returns a device based on the requested free or paid."""
        if source is None:
            source = self._get_combined()

        platforms = {'firefoxos': amo.DEVICE_GAIA,
                     'desktop': amo.DEVICE_DESKTOP,
                     'android-mobile': amo.DEVICE_MOBILE,
                     'android-tablet': amo.DEVICE_TABLET}
        return map(platforms.get, source)

    def is_paid(self):
        return bool(self.cleaned_data.get('paid_platforms', False))

    def get_paid(self):
        """Returns the premium type. Should not be used if the form is used to
        modify an existing app.

        """

        return amo.ADDON_PREMIUM if self.is_paid() else amo.ADDON_FREE


class DevAgreementForm(happyforms.Form):
    read_dev_agreement = forms.BooleanField(label=_lazy(u'Agree and Continue'),
                                            widget=forms.HiddenInput)
    newsletter = forms.BooleanField(required=False, label=app_surveys.label,
                                    widget=forms.CheckboxInput)

    def __init__(self, *args, **kw):
        self.instance = kw.pop('instance')
        self.request = kw.pop('request')
        super(DevAgreementForm, self).__init__(*args, **kw)

    def save(self):
        self.instance.read_dev_agreement = datetime.datetime.now()
        self.instance.save()
        if self.cleaned_data.get('newsletter'):
            UserNotification.update_or_create(user=self.instance,
                notification_id=app_surveys.id, update={'enabled': True})
            basket.subscribe(self.instance.email,
                             'app-dev',
                             format='H',
                             country=self.request.REGION.slug,
                             lang=self.request.LANG,
                             source_url=os.path.join(settings.SITE_URL,
                                                     'developers/submit'))


class NewWebappVersionForm(happyforms.Form):
    upload_error = _lazy(u'There was an error with your upload. '
                         u'Please try again.')
    upload = forms.ModelChoiceField(widget=forms.HiddenInput,
        queryset=FileUpload.objects.filter(valid=True),
        error_messages={'invalid_choice': upload_error})

    def __init__(self, *args, **kw):
        request = kw.pop('request', None)
        self.addon = kw.pop('addon', None)
        self._is_packaged = kw.pop('is_packaged', False)
        super(NewWebappVersionForm, self).__init__(*args, **kw)

        if (not waffle.flag_is_active(request, 'allow-b2g-paid-submission')
            and 'paid_platforms' in self.fields):
            del self.fields['paid_platforms']

    def clean(self):
        data = self.cleaned_data
        if 'upload' not in self.cleaned_data:
            self._errors['upload'] = self.upload_error
            return

        if self.is_packaged():
            # Now run the packaged app check, done in clean, because
            # clean_packaged needs to be processed first.

            try:
                pkg = parse_addon(data['upload'], self.addon)
            except forms.ValidationError, e:
                self._errors['upload'] = self.error_class(e.messages)
                return

            # Collect validation errors so we can display them at once.
            errors = []

            ver = pkg.get('version')
            if (ver and self.addon and
                self.addon.versions.filter(version=ver).exists()):
                errors.append(_(u'Version %s already exists.') % ver)

            origin = pkg.get('origin')
            if origin:
                try:
                    verify_app_domain(origin, packaged=True,
                                      exclude=self.addon)
                except forms.ValidationError, e:
                    errors.append(e.message)

                if self.addon and origin != self.addon.app_domain:
                    errors.append(_('Changes to "origin" are not allowed.'))

            if errors:
                self._errors['upload'] = self.error_class(errors)
                return

        else:
            # Throw an error if this is a dupe.
            # (JS sets manifest as `upload.name`.)
            try:
                verify_app_domain(data['upload'].name)
            except forms.ValidationError, e:
                self._errors['upload'] = self.error_class(e.messages)
                return

        return data

    def is_packaged(self):
        return self._is_packaged


class NewWebappForm(DeviceTypeForm, NewWebappVersionForm):
    upload = forms.ModelChoiceField(widget=forms.HiddenInput,
        queryset=FileUpload.objects.filter(valid=True),
        error_messages={'invalid_choice': _lazy(
            u'There was an error with your upload. Please try again.')})
    packaged = forms.BooleanField(required=False)

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)
        super(NewWebappForm, self).__init__(*args, **kwargs)
        if 'paid_platforms' in self.fields:
            self.fields['paid_platforms'].choices = PAID_PLATFORMS(
                self.request)

    def _add_error(self, msg):
        self._errors['free_platforms'] = self._errors['paid_platforms'] = (
            self.ERRORS[msg])

    def clean(self):
        data = super(NewWebappForm, self).clean()
        if not data:
            return

        if self.is_packaged():
            self._set_packaged_errors()
            if self._errors.get('free_platforms'):
                return

        return data

    def is_packaged(self):
        return self._is_packaged or self.cleaned_data.get('packaged', False)


class AppDetailsBasicForm(TranslationFormMixin, happyforms.ModelForm):
    """Form for "Details" submission step."""

    PUBLISH_CHOICES = (
        (amo.PUBLISH_IMMEDIATE,
         _lazy(u'Publish my app and make it visible to everyone in the '
               u'Marketplace and include it in search results.')),
        (amo.PUBLISH_PRIVATE,
         _lazy(u'Do not publish my app. Notify me and I will adjust app '
               u'visibility after it is approved.')),
    )

    app_slug = forms.CharField(max_length=30,
                               widget=forms.TextInput(attrs={'class': 'm'}))
    description = TransField(
        label=_lazy(u'Description:'),
        help_text=_lazy(u'This description will appear on the details page.'),
        widget=TransTextarea(attrs={'rows': 4}))
    privacy_policy = TransField(
        label=_lazy(u'Privacy Policy:'),
        widget=TransTextarea(attrs={'rows': 6}),
        help_text=_lazy(
            u"A privacy policy that explains what data is transmitted from a "
            u"user's computer and how it is used is required."))
    homepage = TransField.adapt(forms.URLField)(
        label=_lazy(u'Homepage:'), required=False,
        widget=TransInput(attrs={'class': 'full'}),
        help_text=_lazy(
            u'If your app has another homepage, enter its address here.'))
    support_url = TransField.adapt(forms.URLField)(
        label=_lazy(u'Support Website:'), required=False,
        widget=TransInput(attrs={'class': 'full'}),
        help_text=_lazy(
            u'If your app has a support website or forum, enter its address '
            u'here.'))
    support_email = TransField.adapt(forms.EmailField)(
        label=_lazy(u'Support Email:'),
        widget=TransInput(attrs={'class': 'full'}),
        help_text=_lazy(
            u'This email address will be listed publicly on the Marketplace '
            u'and used by end users to contact you with support issues. This '
            u'email address will be listed publicly on your app details page.'))
    flash = forms.TypedChoiceField(
        label=_lazy(u'Does your app require Flash support?'),
        required=False, coerce=lambda x: bool(int(x)),
        initial=0, widget=forms.RadioSelect,
        choices=((1, _lazy(u'Yes')),
                 (0, _lazy(u'No'))))
    notes = forms.CharField(
        label=_lazy(u'Your comments for reviewers'), required=False,
        widget=forms.Textarea(attrs={'rows': 2}),
        help_text=_lazy(
            u'Your app will be reviewed by Mozilla before it becomes publicly '
            u'listed on the Marketplace. Enter any special instructions for '
            u'the app reviewers here.'))
    publish_type = forms.TypedChoiceField(
        label=_lazy(u'Once your app is approved, choose a publishing option:'),
        choices=PUBLISH_CHOICES, initial=amo.PUBLISH_IMMEDIATE,
        widget=forms.RadioSelect())

    class Meta:
        model = Addon
        fields = ('app_slug', 'description', 'privacy_policy', 'homepage',
                  'support_url', 'support_email', 'publish_type')

    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request')
        super(AppDetailsBasicForm, self).__init__(*args, **kwargs)

    def clean_app_slug(self):
        slug = self.cleaned_data['app_slug']
        slug_validator(slug, lower=False)

        if slug != self.instance.app_slug:
            if Webapp.objects.filter(app_slug=slug).exists():
                raise forms.ValidationError(
                    _('This slug is already in use. Please choose another.'))

            if BlacklistedSlug.blocked(slug):
                raise forms.ValidationError(
                    _('The slug cannot be "%s". Please choose another.'
                      % slug))

        return slug.lower()

    def save(self, *args, **kw):
        if self.data['notes']:
            create_comm_note(self.instance, self.instance.versions.latest(),
                             self.request.user, self.data['notes'],
                             note_type=comm.SUBMISSION)
        self.instance = super(AppDetailsBasicForm, self).save(commit=True)
        uses_flash = self.cleaned_data.get('flash')
        af = self.instance.get_latest_file()
        if af is not None:
            af.update(uses_flash=bool(uses_flash))

        return self.instance


class AppFeaturesForm(happyforms.ModelForm):
    class Meta:
        exclude = ['version']
        model = AppFeatures

    def __init__(self, *args, **kwargs):
        super(AppFeaturesForm, self).__init__(*args, **kwargs)
        if self.instance:
            self.initial_features = sorted(self.instance.to_keys())
        else:
            self.initial_features = None

    def all_fields(self):
        """
        Degeneratorizes self.__iter__(), the list of fields on the form. This
        allows further manipulation of fields: to display a subset of fields or
        order them in a specific way.
        """
        return [f for f in self.__iter__()]

    def required_api_fields(self):
        """
        All fields on the form, alphabetically sorted by help text.
        """
        return sorted(self.all_fields(), key=lambda x: x.help_text)

    def get_tooltip(self, field):
        field_id = field.name.split('_', 1)[1].upper()
        return (unicode(APP_FEATURES[field_id].get('description') or '') if
                field_id in APP_FEATURES else None)

    def _changed_features(self):
        old_features = defaultdict.fromkeys(self.initial_features, True)
        old_features = set(unicode(f) for f
                           in AppFeatures(**old_features).to_list())
        new_features = set(unicode(f) for f in self.instance.to_list())

        added_features = new_features - old_features
        removed_features = old_features - new_features
        return added_features, removed_features

    def save(self, *args, **kwargs):
        mark_for_rereview = kwargs.pop('mark_for_rereview', True)
        addon = self.instance.version.addon
        rval = super(AppFeaturesForm, self).save(*args, **kwargs)
        if (self.instance and mark_for_rereview and
                addon.status in amo.WEBAPPS_APPROVED_STATUSES and
                sorted(self.instance.to_keys()) != self.initial_features):
            added_features, removed_features = self._changed_features()
            mark_for_rereview_features_change(addon,
                                              added_features,
                                              removed_features)
        return rval
