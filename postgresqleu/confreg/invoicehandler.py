from django.utils import timezone
from django.conf import settings

from .models import ConferenceRegistration, BulkPayment, PendingAdditionalOrder
from .models import RegistrationWaitlistHistory, PrepaidVoucher
from .models import RegistrationTransferPending
from .util import notify_reg_confirmed
from .util import send_conference_mail, send_conference_notification
from .util import reglog
from .util import make_registration_transfer


class InvoiceProcessor(object):
    # Process invoices once they're getting paid
    #
    # In the case of conference registration, this means that we
    # flag the conference registration as confirmed.
    #
    # Since we lock the registration when the invoice is generated,
    # we don't actually need to verify that nothing has changed.
    #
    # All modifications are already wrapped in a django transaction
    def process_invoice_payment(self, invoice):
        # The processorid field contains our registration id
        try:
            reg = ConferenceRegistration.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            raise Exception("Could not find conference registration %s" % invoice.processorid)

        if reg.payconfirmedat:
            raise Exception("Registration already paid")

        reg.payconfirmedat = timezone.now()
        reg.payconfirmedby = "Invoice paid"
        reg.save(update_fields=['payconfirmedat', 'payconfirmedby'])
        reglog(reg, "Confirmed registration by invoice")
        notify_reg_confirmed(reg)

    # Process an invoice being canceled. This means we need to unlink
    # it from the registration. We don't actually remove the registration,
    # but it will automatically become "unlocked" for further edits.
    def process_invoice_cancellation(self, invoice):
        try:
            reg = ConferenceRegistration.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            raise Exception("Could not find conference registration %s" % invoice.processorid)

        if reg.payconfirmedat:
            raise Exception("Registration already paid")

        if reg.canceledat:
            raise Exception("Registration already canceled")

        # Unlink this invoice from the registration. This will automatically
        # "unlock" the registration
        reg.invoice = None
        reglog(reg, "Invoice #{} canceled, unlinking from reg".format(invoice.id))

        # If this registration holds any additional options that are about to expire, release
        # them for others to use at this point.
        for ao in reg.additionaloptions.filter(invoice_autocancel_hours__isnull=False, invoice_autocancel_hours__gt=0):
            reglog(reg, "Expired additional option {}".format(ao.name))
            reg.additionaloptions.remove(ao)

        reg.save(update_fields=['invoice'])

        # If the registration was on the waitlist, put it back in the
        # queue.
        if hasattr(reg, 'registrationwaitlistentry'):
            wl = reg.registrationwaitlistentry
            RegistrationWaitlistHistory(waitlist=wl,
                                        text="Invoice was cancelled, moving back to waitlist").save()
            wl.offeredon = None
            wl.offerexpires = None
            wl.enteredon = timezone.now()
            wl.save()

            send_conference_notification(
                reg.conference,
                'Waitlist invoice canceled',
                'Invoice for user {0} {1} <{2}> was canceled, and the offer has expired.'.format(reg.firstname, reg.lastname, reg.email),
            )

        # If the registration was attached to a discount code, remove it so that it is no
        # longer counted against it. Also clear out the field, in case others want to use
        # that discount code.
        if reg.discountcode_set.exists():
            reg.discountcode_set.clear()
        if reg.vouchercode:
            try:
                vc = PrepaidVoucher.objects.get(vouchervalue=reg.vouchercode)
                vc.usedate = None
                vc.user = None
                vc.save(update_fields=['usedate', 'user'])
            except PrepaidVoucher.DoesNotExist:
                # Vouchercode is set even if it's a discount code, since we use the same field.
                # And in this case, there is no matching prepaid voucher.
                pass

            reg.vouchercode = ''
            reg.save(update_fields=['vouchercode'])

    # Return the user to a page showing what happened as a result
    # of their payment. In our case, we just return the user directly
    # to the registration page.
    def get_return_url(self, invoice):
        # The processorid field contains our registration id
        try:
            reg = ConferenceRegistration.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            raise Exception("Could not find conference registration %s" % invoice.processorid)
        return "%s/events/%s/register/" % (settings.SITEBASE, reg.conference.urlname)

    # Admin access to the registration
    def get_admin_url(self, invoice):
        try:
            reg = ConferenceRegistration.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            return None
        return "/events/admin/{0}/regdashboard/list/{1}/".format(reg.conference.urlname, reg.pk)


class BulkInvoiceProcessor(object):
    # Process invoices once they're getting paid
    #
    # In the case of conference bulk registrations, this means that we
    # flag all the related conference registrations as confirmed.
    #
    # Since we lock the registration when the invoice is generated,
    # we don't actually need to verify that nothing has changed.
    #
    # All modifications are already wrapped in a django transaction
    def process_invoice_payment(self, invoice):
        # The processorid field contains our bulkpayment id
        try:
            bp = BulkPayment.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            raise Exception("Could not find bulk payment %s" % invoice.processorid)

        if bp.paidat:
            raise Exception("Bulk payment already paid")

        bp.paidat = timezone.now()

        # Confirm all related ones
        for r in bp.conferenceregistration_set.all():
            r.payconfirmedat = timezone.now()
            r.payconfirmedby = "Bulk paid"
            r.save(update_fields=['payconfirmedat', 'payconfirmedby'])
            reglog(r, "Confirmed registration by bulk paid")
            notify_reg_confirmed(r)

        bp.save(update_fields=['paidat'])

    # Process an invoice being canceled. This means we need to unlink
    # it from the registration. We don't actually remove the registration,
    # but it will automatically become "unlocked" for further edits.
    def process_invoice_cancellation(self, invoice):
        try:
            bp = BulkPayment.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            raise Exception("Could not find bulk payment %s" % invoice.processor)
        if bp.paidat:
            raise Exception("Bulk registration already paid")

        # Unlink this bulk payment from all registrations. This will
        # automatically unlock the registrations. Also notify the
        # attendees that this happened.
        for r in bp.conferenceregistration_set.all():
            r.bulkpayment = None
            r.save(update_fields=['bulkpayment'])
            reglog(r, "Unlinked from bulk payment by cancel")

            if r.attendee:
                # Only notify if this attendee actually knows about the
                # registration.
                send_conference_mail(bp.conference,
                                     r.email,
                                     "Your multi-registration canceled",
                                     'confreg/mail/bulkpay_canceled.txt',
                                     {
                                         'conference': bp.conference,
                                         'reg': r,
                                         'bulk': bp,
                                     },
                                     receivername=r.fullname,
                )

            # If this registration holds any additional options that are about to expire, release
            # them for others to use at this point.
            for ao in r.additionaloptions.filter(invoice_autocancel_hours__isnull=False, invoice_autocancel_hours__gt=0):
                reglog(r, "Expired additional option {}".format(ao.name))
                r.additionaloptions.remove(ao)

            # If the registration was attached to a discount code, remove it so that it is no
            # longer counted against it. Also clear out the field, in case others want to use
            # that discount code.
            if r.discountcode_set.exists():
                # If this discountcode is in the vouchercode field, clear it.
                dcodes = r.discountcode_set.all()
                if len(dcodes) != 1:
                    raise Exception("Matched {} discount codes, not 1!".format(len(dcodes)))
                if dcodes[0].code == r.vouchercode:
                    r.vouchercode = ''
                r.discountcode_set.clear()
                r.save(update_fields=['vouchercode'])

            # If there is still a voucher code, it must be referring to a regular voucher
            # and not a discount code.
            if r.vouchercode:
                # Also mark the voucher code as not used anymore
                try:
                    vc = PrepaidVoucher.objects.get(vouchervalue=r.vouchercode)
                    vc.usedate = None
                    vc.user = None
                    vc.save(update_fields=['usedate', 'user'])
                except PrepaidVoucher.DoesNotExist:
                    # Vouchercode is set even if it's a discount code, since we use the same field.
                    # And in this case, there is no matching prepaid voucher.
                    pass

                r.vouchercode = ''
                r.save(update_fields=['vouchercode'])

        # Now actually *remove* the bulk payment record completely,
        # since it no longer contains anything interesting.
        bp.delete()

    # Return the user to a page showing what happened as a result
    # of their payment. In our case, we just return the user directly
    # to the bulk payment page.
    def get_return_url(self, invoice):
        try:
            bp = BulkPayment.objects.get(pk=invoice.processorid)
        except ConferenceRegistration.DoesNotExist:
            raise Exception("Could not find bulk payment %s" % invoice.processor)
        return "%s/events/%s/register/other/" % (settings.SITEBASE, bp.conference.urlname)

    # Admin access to the bulk payment we just send to the dashboard
    def get_admin_url(self, invoice):
        try:
            bp = BulkPayment.objects.get(pk=invoice.processorid)
        except BulkPayment.DoesNotExist:
            return None
        return "/events/admin/{0}/multiregs/?b={1}".format(bp.conference.urlname, bp.id)


class AddonInvoiceProcessor(object):
    can_refund = False
    # Process invoices for additional options added to an existing
    # registration.
    #
    # Since we lock the registration when the invoice is generated,
    # we don't actually need to verify that nothing has changed.
    #
    # All modifications are already wrapped in a django transaction

    def process_invoice_payment(self, invoice):
        try:
            order = PendingAdditionalOrder.objects.get(pk=invoice.processorid)
        except PendingAdditionalOrder.DoesNotExist:
            raise Exception("Could not find additional options order %s!" % invoice.processorid)

        if order.payconfirmedat:
            raise Exception("Additional options already paid")

        order.payconfirmedat = timezone.now()
        if order.newregtype:
            order.reg.regtype = order.newregtype

        for o in order.options.all():
            order.reg.additionaloptions.add(o)

        order.reg.save(update_fields=['regtype'])
        order.save()

    def process_invoice_cancellation(self, invoice):
        try:
            order = PendingAdditionalOrder.objects.get(pk=invoice.processorid)
        except PendingAdditionalOrder.DoesNotExist:
            raise Exception("Could not find additional options order %s!" % invoice.processorid)

        # We just remove the entry completely, as there is no "unlocking"
        # here.
        order.delete()

    # Return the user to their dashboard
    def get_return_url(self, invoice):
        try:
            order = PendingAdditionalOrder.objects.get(pk=invoice.processorid)
        except PendingAdditionalOrder.DoesNotExist:
            raise Exception("Could not find additional options order %s!" % invoice.processorid)

        return "%s/events/%s/register/" % (settings.SITEBASE, order.reg.conference.urlname)

    # Admin access to the registration
    def get_admin_url(self, invoice):
        try:
            order = PendingAdditionalOrder.objects.get(pk=invoice.processorid)
        except PendingAdditionalOrder.DoesNotExist:
            return None
        return "/events/admin/{0}/regdashboard/list/{1}/".format(order.reg.conference.urlname, order.reg.pk)


class TransferInvoiceProcessor(object):
    can_refund = False
    # Process invoices for registration transfers.
    #
    # All modifications are already wrapped in a django transaction

    def process_invoice_payment(self, invoice):
        try:
            pending = RegistrationTransferPending.objects.get(pk=invoice.processorid)
        except RegistrationTransferPending.DoesNotExist:
            raise Exception("Could not find pending transfer: %s!" % invoice.processorid)

        reglog(pending.fromreg, 'Paid invoice for pending transfer')
        dummy = list(make_registration_transfer(pending.fromreg, pending.toreg, None, True))

        pending.delete()

    def process_invoice_cancellation(self, invoice):
        try:
            pending = RegistrationTransferPending.objects.get(pk=invoice.processorid)
        except RegistrationTransferPending.DoesNotExist:
            raise Exception("Could not find pending transfer: %s!" % invoice.processorid)

        reglog(pending.fromreg, 'Canceled invoice for pending transfer')
        pending.delete()

    # Return the user to their dashboard
    def get_return_url(self, invoice):
        try:
            pending = RegistrationTransferPending.objects.get(pk=invoice.processorid)
        except RegistrationTransferPending.DoesNotExist:
            # We can't find the transfer - that could be because it's successfully completed,
            # at which point the reservation may be canceled. So in this case, we just redirect
            # the user back to their invoice, because we have to send them somewhere.
            return "{}/invoices/{}/".format(settings.SITEBASE, invoice.recipient_secret)

        return "{}/events/{}/".format(settings.SITEBASE, pending.conference.urlname)

    def get_admin_url(self, invoice):
        try:
            pending = RegistrationTransferPending.objects.get(pk=invoice.processorid)
        except RegistrationTransferPending.DoesNotExist:
            return None

        return "/events/admin/{0}/transfer/".format(pending.conference.urlname)
