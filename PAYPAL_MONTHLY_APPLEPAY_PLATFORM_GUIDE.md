# PayPal & Monthly Donations - Executive Guide

> **Prepared For:** Mr. Usman  
> **Purpose:** Overview of PayPal Multi-Accounts, Monthly Donations (Apple Pay / Card / Google Pay), and Platform Admin Features.

---

## 1. Smooth Monthly Donations (No Redirects)

All campaigns now support **Monthly Donations** directly inside the donation widget—just like `s.fundraiseup.us.com`.

- **No Page Redirects:** Donors do not get bounced away to external websites. Everything happens smoothly right inside the donation popup.
- **Apple Pay & Google Pay:** On iPhone and Android, donors can set up monthly donations with a single touch using Face ID, Touch ID, or fingerprint.
- **Credit & Debit Cards:** Donors can enter their card details directly in the popup for recurring monthly giving.
- **Automated Renewals:** Once authorized, the monthly donation is securely saved and automatically renewed each month without the donor having to take any further action.

---

## 2. Three PayPal Accounts

To keep campaign funds organized and separated, the platform operates with three dedicated PayPal accounts:

| Account Name | Previous Label | Role & Purpose | Connected Campaigns |
| :--- | :--- | :--- | :--- |
| **Paypal Main** | PayPal 1 | **Platform Default** | General campaigns and **Empty Plates** (currently active). |
| **Paypal--S** | PayPal 2 | **Hope for Gaza Dedicated** | Exclusively handles donations for **Hope for Gaza Foundation**. |
| **Paypal--Z** | PayPal 3 | **Secondary Dedicated** | Standby dedicated PayPal account (can be assigned anytime). |

### Current Routing Setup
- **Empty Plates Campaign:** Configured to deposit into **Paypal Main** for now.
- **Hope for Gaza Campaign:** Automatically deposits into **Paypal--S**.
- **All other campaigns:** Deposit into **Paypal Main**.
- You can reassign any campaign to any account at any time using the **PayPal Accounts** tab in Platform Admin.

---

## 3. New "PayPal Accounts" Tab in Platform Admin

A dedicated management tab is now available in the Platform Admin console:

- **Where to Find It:** Log in as Super Admin $\rightarrow$ Switch to **Platform Admin** $\rightarrow$ Click **PayPal Accounts** in the left menu.
- **Live Status:** Check if all three accounts are active and connected.
- **Update Keys:** Easily update your PayPal Client ID and Secret directly from the dashboard whenever needed.
- **Reassign Campaigns:** Use a simple dropdown next to any campaign to switch it to **Paypal Main**, **Paypal--S**, or **Paypal--Z** instantly.

---

## 4. PayPal Labels & Badges on Donations

Every PayPal donation in your platform now clearly displays which PayPal account received the money:

- **Paypal Main** $\rightarrow$ Gray badge
- **Paypal--S** $\rightarrow$ Green badge
- **Paypal--Z** $\rightarrow$ Blue badge

These badges are visible in the **Donations** list next to the payment method icon so you can see at a glance where funds landed.

---

## 5. Filter Donations by PayPal Account

In the **Donations** tab, you can now filter transactions by PayPal account using the dropdown filter:
- **All PayPal accounts** (Shows everything)
- **Paypal Main**
- **Paypal--S**
- **Paypal--Z**

### Works for Past Donations Too
The filter is smart: when you select **Paypal--S**, it shows all current donations as well as older historical donations (previously marked as PayPal 2). The same applies to **Paypal Main** (PayPal 1) and **Paypal--Z** (PayPal 3).

---

## 6. Common Question: Why Would a Donation Show in PayPal but Not in the Portal?

*(Example: The £22 donation on Empty Plates)*

- **How it works:** When a donor clicks "Donate with PayPal", PayPal creates a pending checkout order.
- **If the donor closes the window:** If a visitor opens PayPal but changes their mind or closes their browser before entering their password or clicking "Confirm/Pay", no money was actually charged.
- **Portal accuracy:** The platform only displays **completed, successful donations** so your financial reports match the real money in your bank.
- **Safety net:** If a donor *did* successfully pay but closed their tab before returning to your site, our automatic background system detects the payment from PayPal within seconds and adds it to your platform automatically.
