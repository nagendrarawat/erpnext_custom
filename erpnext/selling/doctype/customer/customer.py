# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe
from frappe.model.naming import set_name_by_naming_series
from frappe import _, msgprint, throw
import frappe.defaults
from frappe.utils import flt, cint, cstr
from frappe.desk.reportview import build_match_conditions, get_filters_cond
from erpnext.utilities.transaction_base import TransactionBase
#from erpnext.accounts.party import validate_party_accounts, get_dashboard_info, get_timeline_data # keep this
from frappe.contacts.address_and_contact import load_address_and_contact, delete_contact_and_address
from frappe.model.rename_doc import update_linked_doctypes

from six import iteritems

def get_timeline_data(doctype, name):
	'''returns timeline data for the past one year'''
	from frappe.desk.form.load import get_communication_data

	out = {}
	fields = 'date(creation), count(name)'
	after = add_years(None, -1).strftime('%Y-%m-%d')
	group_by='group by date(creation)'

	data = get_communication_data(doctype, name,
		fields=fields, after=after, group_by=group_by, as_dict=False)

	# fetch and append data from Activity Log
	data += frappe.db.sql("""select {fields}
		from `tabActivity Log`
		where reference_doctype="{doctype}" and reference_name="{name}"
		and status!='Success' and creation > {after}
		{group_by} order by creation desc
		""".format(doctype=frappe.db.escape(doctype), name=frappe.db.escape(name), fields=fields,
			group_by=group_by, after=after), as_dict=False)

	timeline_items = dict(data)

	for date, count in iteritems(timeline_items):
		timestamp = get_timestamp(date)
		out.update({ timestamp: count })

	return out

class Customer(TransactionBase):
	def get_feed(self):
		return self.customer_name

	def onload(self):
		"""Load address and contacts in `__onload`"""
		load_address_and_contact(self)
		self.load_dashboard_info()

	#def load_dashboard_info(self):
		#info = get_dashboard_info(self.doctype, self.name)
		#self.set_onload('dashboard_info', info)

	def autoname(self):
		cust_master_name = frappe.defaults.get_global_default('cust_master_name')
		if cust_master_name == 'Customer Name':
			self.name = self.get_customer_name()
		else:
			set_name_by_naming_series(self)

	def get_customer_name(self):
		if frappe.db.get_value("Customer", self.customer_name):
			count = frappe.db.sql("""select ifnull(MAX(CAST(SUBSTRING_INDEX(name, ' ', -1) AS UNSIGNED)), 0) from tabCustomer
				 where name like %s""", "%{0} - %".format(self.customer_name), as_list=1)[0][0]
			count = cint(count) + 1
			return "{0} - {1}".format(self.customer_name, cstr(count))

		return self.customer_name

	#def after_insert(self):
		#'''If customer created from Lead, update customer id in quotations, opportunities'''
		#self.update_lead_status()

	def validate(self):
		self.flags.is_new_doc = self.is_new()
		self.flags.old_lead = self.lead_name
		#validate_party_accounts(self)
		self.validate_credit_limit_on_change()
		self.check_customer_group_change()

	def check_customer_group_change(self):
		frappe.flags.customer_group_changed = False

		if not self.get('__islocal'):
			if self.customer_group != frappe.db.get_value('Customer', self.name, 'customer_group'):
				frappe.flags.customer_group_changed = True

	def on_update(self):
		self.validate_name_with_customer_group()
		self.create_primary_contact()
		self.create_primary_address()

		#if self.flags.old_lead != self.lead_name:
			#self.update_lead_status()

		#if self.flags.is_new_doc:
			#self.create_lead_address_contact()

		self.update_customer_groups()

	def update_customer_groups(self):
		ignore_doctypes = ["Lead", "Opportunity", "POS Profile", "Tax Rule", "Pricing Rule"]
		if frappe.flags.customer_group_changed:
			update_linked_doctypes('Customer', frappe.db.escape(self.name), 'Customer Group',
				self.customer_group, ignore_doctypes)

	def create_primary_contact(self):
		if not self.customer_primary_contact and not self.lead_name:
			if self.mobile_no or self.email_id:
				contact = make_contact(self)
				self.db_set('customer_primary_contact', contact.name)
				self.db_set('mobile_no', self.mobile_no)
				self.db_set('email_id', self.email_id)

	def create_primary_address(self):
		if self.flags.is_new_doc and self.get('address_line1'):
			make_address(self)

	
	def validate_name_with_customer_group(self):
		if frappe.db.exists("Customer Group", self.name):
			frappe.throw(_("A Customer Group exists with same name please change the Customer name or rename the Customer Group"), frappe.NameError)

	def validate_credit_limit_on_change(self):
		if self.get("__islocal") or not self.credit_limit \
			or self.credit_limit == frappe.db.get_value("Customer", self.name, "credit_limit"):
			return

		for company in frappe.get_all("Company"):
			outstanding_amt = get_customer_outstanding(self.name, company.name)
			if flt(self.credit_limit) < outstanding_amt:
				frappe.throw(_("""New credit limit is less than current outstanding amount for the customer. Credit limit has to be atleast {0}""").format(outstanding_amt))

	#def on_trash(self):
		#delete_contact_and_address('Customer', self.name)
		#if self.lead_name:
			#frappe.db.sql("update `tabLead` set status='Interested' where name=%s", self.lead_name)

	def after_rename(self, olddn, newdn, merge=False):
		if frappe.defaults.get_global_default('cust_master_name') == 'Customer Name':
			frappe.db.set(self, "customer_name", newdn)


def get_customer_list(doctype, txt, searchfield, start, page_len, filters=None):
	if frappe.db.get_default("cust_master_name") == "Customer Name":
		fields = ["name", "customer_group", "territory"]
	else:
		fields = ["name", "customer_name", "customer_group", "territory"]

	match_conditions = build_match_conditions("Customer")
	match_conditions = "and {}".format(match_conditions) if match_conditions else ""

	if filters:
		filter_conditions = get_filters_cond(doctype, filters, [])
		match_conditions += "{}".format(filter_conditions)

	return frappe.db.sql("""select %s from `tabCustomer` where docstatus < 2
		and (%s like %s or customer_name like %s)
		{match_conditions}
		order by
		case when name like %s then 0 else 1 end,
		case when customer_name like %s then 0 else 1 end,
		name, customer_name limit %s, %s""".format(match_conditions=match_conditions) %
		(", ".join(fields), searchfield, "%s", "%s", "%s", "%s", "%s", "%s"),
		("%%%s%%" % txt, "%%%s%%" % txt, "%%%s%%" % txt, "%%%s%%" % txt, start, page_len))


def check_credit_limit(customer, company, ignore_outstanding_sales_order=False, extra_amount=0):
	customer_outstanding = get_customer_outstanding(customer, company, ignore_outstanding_sales_order)
	if extra_amount > 0:
		customer_outstanding += flt(extra_amount)

	credit_limit = get_credit_limit(customer, company)
	if credit_limit > 0 and flt(customer_outstanding) > credit_limit:
		msgprint(_("Credit limit has been crossed for customer {0} ({1}/{2})")
			.format(customer, customer_outstanding, credit_limit))

		# If not authorized person raise exception
		#credit_controller = frappe.db.get_value('Accounts Settings', None, 'credit_controller')
		#if not credit_controller or credit_controller not in frappe.get_roles():
			#throw(_("Please contact to the user who have Sales Master Manager {0} role")
				#.format(" / " + credit_controller if credit_controller else ""))
'''
def get_customer_outstanding(customer, company, ignore_outstanding_sales_order=False):
	# Outstanding based on GL Entries
	#outstanding_based_on_gle = frappe.db.sql("""
		#select sum(debit) - sum(credit)
		#from `tabGL Entry`
		#where party_type = 'Customer' and party = %s and company=%s""", (customer, company))

	outstanding_based_on_gle = flt(outstanding_based_on_gle[0][0]) if outstanding_based_on_gle else 0

	# Outstanding based on Sales Order
	outstanding_based_on_so = 0.0

	# if credit limit check is bypassed at sales order level,
	# we should not consider outstanding Sales Orders, when customer credit balance report is run
	if not ignore_outstanding_sales_order:
		outstanding_based_on_so = frappe.db.sql("""
			select sum(base_grand_total*(100 - per_billed)/100)
			from `tabSales Order`
			where customer=%s and docstatus = 1 and company=%s
			and per_billed < 100 and status != 'Closed'""", (customer, company))

		outstanding_based_on_so = flt(outstanding_based_on_so[0][0]) if outstanding_based_on_so else 0.0

	# Outstanding based on Delivery Note, which are not created against Sales Order
	unmarked_delivery_note_items = frappe.db.sql("""select
			dn_item.name, dn_item.amount, dn.base_net_total, dn.base_grand_total
		from `tabDelivery Note` dn, `tabDelivery Note Item` dn_item
		where
			dn.name = dn_item.parent
			and dn.customer=%s and dn.company=%s
			and dn.docstatus = 1 and dn.status not in ('Closed', 'Stopped')
			and ifnull(dn_item.against_sales_order, '') = ''
			and ifnull(dn_item.against_sales_invoice, '') = ''
		""", (customer, company), as_dict=True)

	outstanding_based_on_dn = 0.0

	for dn_item in unmarked_delivery_note_items:
		si_amount = frappe.db.sql("""select sum(amount)
			from `tabSales Invoice Item`
			where dn_detail = %s and docstatus = 1""", dn_item.name)[0][0]

		if flt(dn_item.amount) > flt(si_amount) and dn_item.base_net_total:
			outstanding_based_on_dn += ((flt(dn_item.amount) - flt(si_amount)) \
				/ dn_item.base_net_total) * dn_item.base_grand_total

	return outstanding_based_on_gle + outstanding_based_on_so + outstanding_based_on_dn

'''
def get_credit_limit(customer, company):
	credit_limit = None

	if customer:
		credit_limit, customer_group = frappe.db.get_value("Customer",
			customer, ["credit_limit", "customer_group"])

		if not credit_limit:
			credit_limit = frappe.db.get_value("Customer Group", customer_group, "credit_limit")

	if not credit_limit:
		credit_limit = frappe.db.get_value("Company", company, "credit_limit")

	return flt(credit_limit)

def make_contact(args, is_primary_contact=1):
	contact = frappe.get_doc({
		'doctype': 'Contact',
		'first_name': args.get('name'),
		'mobile_no': args.get('mobile_no'),
		'email_id': args.get('email_id'),
		'is_primary_contact': is_primary_contact,
		'links': [{
			'link_doctype': args.get('doctype'),
			'link_name': args.get('name')
		}]
	}).insert()

	return contact

def make_address(args, is_primary_address=1):
	address = frappe.get_doc({
		'doctype': 'Address',
		'address_title': args.get('name'),
		'address_line1': args.get('address_line1'),
		'address_line2': args.get('address_line2'),
		'city': args.get('city'),
		'state': args.get('state'),
		'pincode': args.get('pincode'),
		'country': args.get('country'),
		'links': [{
			'link_doctype': args.get('doctype'),
			'link_name': args.get('name')
		}]
	}).insert()

	return address

def get_customer_primary_contact(doctype, txt, searchfield, start, page_len, filters):
	customer = filters.get('customer')
	return frappe.db.sql("""
		select `tabContact`.name from `tabContact`, `tabDynamic Link`
			where `tabContact`.name = `tabDynamic Link`.parent and `tabDynamic Link`.link_name = %(customer)s
			and `tabDynamic Link`.link_doctype = 'Customer' and `tabContact`.is_primary_contact = 1
			and `tabContact`.name like %(txt)s
		""", {
			'customer': customer,
			'txt': '%%%s%%' % txt
		})

def get_customer_primary_address(doctype, txt, searchfield, start, page_len, filters):
	customer = frappe.db.escape(filters.get('customer'))
	return frappe.db.sql("""
		select `tabAddress`.name from `tabAddress`, `tabDynamic Link`
			where `tabAddress`.name = `tabDynamic Link`.parent and `tabDynamic Link`.link_name = %(customer)s
			and `tabDynamic Link`.link_doctype = 'Customer' and `tabAddress`.is_primary_address = 1
			and `tabAddress`.name like %(txt)s
		""", {
			'customer': customer,
			'txt': '%%%s%%' % txt
		})