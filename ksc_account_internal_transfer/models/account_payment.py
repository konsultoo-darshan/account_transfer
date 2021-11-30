# -*- coding: utf-8 -*-

from odoo import fields, models, api, _
from odoo.exceptions import UserError


class AccountPayment(models.Model):
    _inherit = "account.payment"

    payment_type = fields.Selection(selection_add=[('transfer', 'Internal Transfer')],
                                    ondelete={'transfer': 'set default'})
    destination_journal_id = fields.Many2one('account.journal', string='Transfer To',
                                             domain="[('type', 'in', ('bank', 'cash'))]", readonly=True,
                                             states={'draft': [('readonly', False)]})
    payment_type_mode = fields.Selection([('outbound', 'Send Money'), ('inbound', 'Receive Money')], string="Payment Type")

    @api.model
    def default_get(self, default_fields):
        res = super().default_get(default_fields)
        if res.get('payment_type') != 'transfer':
            res['payment_type_mode'] = res.get('payment_type')
        return res

    @api.onchange('payment_type_mode')
    def set_payment_type(self):
        self.payment_type = self.payment_type_mode

    @api.model
    def create(self, vals):
        res = super(AccountPayment, self).create(vals)
        if vals.get('payment_type') == 'transfer':
            res.ensure_one()
            balance = vals.get('amount')
            move = self.env['account.move'].create({
                'date': res.date,
                'ref': '',
                'partner_id': False,
                'journal_id': res.journal_id.id,
                'payment_id': res.id,
                'line_ids': [
                    (0, 0, {
                        'name': _('Transfer to %s') % res.destination_journal_id.name,
                        'amount_currency': 0.0,
                        'currency_id': res.currency_id.id,
                        'debit': balance < 0.0 and -balance or 0.0,
                        'credit': balance > 0.0 and balance or 0.0,
                        'date_maturity': res.date,
                        'partner_id': False,
                        'account_id': res.journal_id.default_account_id.id,
                        'payment_id': res.id,
                    }),
                    (0, 0, {
                        'name': _('Internal Transfer from %s') % res.journal_id.name,
                        'amount_currency': 0.0,
                        'currency_id': res.currency_id.id,
                        'debit': balance > 0.0 and balance or 0.0,
                        'credit': balance < 0.0 and -balance or 0.0,
                        'date_maturity': res.date,
                        'partner_id': False,
                        'account_id': res.company_id.transfer_account_id.id,
                        'payment_id': res.id,
                    }),
                ],
            })
            self.env['account.move'].create(
                {
                    'date': res.date,
                    'ref': False,
                    'partner_id': False,
                    'journal_id': res.destination_journal_id.id,
                    'payment_id': res.id,
                    'line_ids': [
                        (0, 0, {
                            'name': _('Internal Transfer to %s') % res.destination_journal_id.name,
                            'amount_currency': 0.0,
                            'currency_id': res.currency_id.id,
                            'debit': balance < 0.0 and -balance or 0.0,
                            'credit': balance > 0.0 and balance or 0.0,
                            'date_maturity': res.date,
                            'partner_id': False,
                            'account_id': res.company_id.transfer_account_id.id,
                            'payment_id': res.id,
                        }),
                        (0, 0, {
                            'name': _('Transfer from %s') % res.journal_id.name,
                            'amount_currency': 0.0,
                            'currency_id': res.currency_id.id,
                            'debit': balance > 0.0 and balance or 0.0,
                            'credit': balance < 0.0 and -balance or 0.0,
                            'date_maturity': res.date,
                            'partner_id': False,
                            'account_id': res.destination_journal_id.default_account_id.id,
                            'payment_id': res.id,
                        }),
                    ],
                })
            res.amount = balance
            res.move_id.line_ids.unlink()
            temp = res.move_id
            res.move_id = move
            temp.unlink()
        return res

    def action_view_journal_entries(self):
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_move_journal_line")
        action['domain'] = [('payment_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_view_journal_items(self):
        action = self.env["ir.actions.actions"]._for_xml_id("account.action_account_moves_all_tree")
        action['domain'] = [('payment_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_post(self):
        for rec in self:
            move = self.env['account.move'].search(
                [('payment_id', '=', rec.id), ('state', '=', 'draft'), ('id', '!=', rec.move_id.id)])
            move.action_post()
        res = super(AccountPayment, self).action_post()
        for rec in self:
            if rec.payment_type == 'transfer':
                rec.with_context(skip_account_move_synchronization=True).write(
                    {'destination_account_id': rec.journal_id.company_id.transfer_account_id})
                accounts = list(move.line_ids.account_id + rec.move_id.line_ids.account_id)
                reconcile_account = list([x for x in accounts if accounts.count(x) > 1])
                move_lines = move.line_ids + rec.move_id.line_ids
                reconcile_line = move_lines.filtered(lambda line: line.account_id == reconcile_account[0])
                reconcile_line.reconcile()
        return res

    def action_draft(self):
        for rec in self:
            self.env['account.move'].search(
                [('payment_id', '=', rec.id), ('state', '=', 'posted'), ('id', '!=', rec.move_id.id)]).button_draft()
        return super(AccountPayment, self).action_draft()

    def _seek_for_lines(self):
        ''' Helper used to dispatch the journal items between:
        - The lines using the temporary liquidity account.
        - The lines using the counterpart account.
        - The lines being the write-off lines.
        :return: (liquidity_lines, counterpart_lines, writeoff_lines)
        '''
        self.ensure_one()

        liquidity_lines = self.env['account.move.line']
        counterpart_lines = self.env['account.move.line']
        writeoff_lines = self.env['account.move.line']

        for line in self.move_id.line_ids:
            if line.account_id in (
                    self.journal_id.default_account_id,
                    self.journal_id.payment_debit_account_id,
                    self.journal_id.payment_credit_account_id,
            ):
                liquidity_lines += line
            elif line.account_id.internal_type in (
            'receivable', 'payable', 'other') or line.partner_id == line.company_id.partner_id:
                counterpart_lines += line
            else:
                writeoff_lines += line

        return liquidity_lines, counterpart_lines, writeoff_lines

    @api.depends('partner_id', 'destination_account_id', 'journal_id')
    def _compute_is_internal_transfer(self):
        for payment in self:
            is_partner_ok = payment.partner_id == payment.journal_id.company_id.partner_id
            is_account_ok = payment.destination_account_id and payment.destination_account_id == payment.journal_id.company_id.transfer_account_id
            payment.is_internal_transfer = is_partner_ok and is_account_ok
            if payment.payment_type == 'transfer':
                payment.is_internal_transfer = True

    def _synchronize_from_moves(self, changed_fields):
        ''' Update the account.payment regarding its related account.move.
        Also, check both models are still consistent.
        :param changed_fields: A set containing all modified fields on account.move.
        '''
        if self._context.get('skip_account_move_synchronization'):
            return

        for pay in self.with_context(skip_account_move_synchronization=True):

            # After the migration to 14.0, the journal entry could be shared between the account.payment and the
            # account.bank.statement.line. In that case, the synchronization will only be made with the statement line.
            if pay.move_id.statement_line_id:
                continue

            move = pay.move_id
            move_vals_to_write = {}
            payment_vals_to_write = {}

            if 'journal_id' in changed_fields:
                if pay.journal_id.type not in ('bank', 'cash'):
                    raise UserError(_("A payment must always belongs to a bank or cash journal."))

            if 'line_ids' in changed_fields:
                all_lines = move.line_ids
                liquidity_lines, counterpart_lines, writeoff_lines = pay._seek_for_lines()

                if len(liquidity_lines) != 1 or len(counterpart_lines) != 1:
                    raise UserError(_(
                        "The journal entry %s reached an invalid state relative to its payment.\n"
                        "To be consistent, the journal entry must always contains:\n"
                        "- one journal item involving the outstanding payment/receipts account.\n"
                        "- one journal item involving a receivable/payable account.\n"
                        "- optional journal items, all sharing the same account.\n\n"
                    ) % move.display_name)

                if writeoff_lines and len(writeoff_lines.account_id) != 1:
                    raise UserError(_(
                        "The journal entry %s reached an invalid state relative to its payment.\n"
                        "To be consistent, all the write-off journal items must share the same account."
                    ) % move.display_name)

                if any(line.currency_id != all_lines[0].currency_id for line in all_lines):
                    raise UserError(_(
                        "The journal entry %s reached an invalid state relative to its payment.\n"
                        "To be consistent, the journal items must share the same currency."
                    ) % move.display_name)

                if any(line.partner_id != all_lines[0].partner_id for line in all_lines):
                    raise UserError(_(
                        "The journal entry %s reached an invalid state relative to its payment.\n"
                        "To be consistent, the journal items must share the same partner."
                    ) % move.display_name)

                if counterpart_lines.account_id.user_type_id.type == 'receivable':
                    partner_type = 'customer'
                else:
                    partner_type = 'supplier'

                liquidity_amount = liquidity_lines.amount_currency

                move_vals_to_write.update({
                    'currency_id': liquidity_lines.currency_id.id,
                    'partner_id': liquidity_lines.partner_id.id,
                })
                payment_vals_to_write.update({
                    'amount': abs(liquidity_amount),
                    'partner_type': partner_type,
                    'currency_id': liquidity_lines.currency_id.id,
                    'destination_account_id': counterpart_lines.account_id.id,
                    'partner_id': liquidity_lines.partner_id.id,
                })
                if liquidity_amount > 0.0 and pay.payment_type != 'transfer':
                    payment_vals_to_write.update({'payment_type': 'inbound'})
                elif liquidity_amount < 0.0 and pay.payment_type != 'transfer':
                    payment_vals_to_write.update({'payment_type': 'outbound'})

            move.write(move._cleanup_write_orm_values(move, move_vals_to_write))
            pay.write(move._cleanup_write_orm_values(pay, payment_vals_to_write))


class account_journal(models.Model):
    _inherit = "account.journal"
    def open_transfer_money(self):
        super(account_journal, self).open_transfer_money()
        action = self.open_payments_action('transfer')
        action['context'].update({'internal_transfer': True,
                               'default_payment_type': 'transfer'})
        return action

    def create_internal_transfer(self):
        res = super(account_journal, self).create_internal_transfer()
        res['context'].update({'internal_transfer': True,
                               'default_payment_type': 'transfer'})
        return res
