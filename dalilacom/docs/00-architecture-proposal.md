# DALILACOM — دليلكم
## المرحلة 1: وثيقة التحليل والمعمارية المقترحة (Phase 1–11 Proposal)

> **حالة هذه الوثيقة:** تحليل وتصميم فقط — لا يوجد أي كود منفَّذ بعد. هذا تنفيذ للطلب الصريح في القسم 30 من البرومبت: "لا تبدأ بكتابة آلاف الأسطر مباشرة، أولًا حلّل ثم اعرض المعمارية."
>
> **القرارات المؤكدة من صاحب المشروع (تم اعتمادها بالسؤال المباشر):**
> 1. DALILACOM مشروع **مستقل تمامًا** عن `express-print-saas` (لا علاقة تقنية أو تجارية بينهما)، لكنه يعيش داخل نفس الـ Git repository ضمن مجلد جديد `dalilacom/` حتى لا نلمس كود المشروع القائم.
> 2. الـ Backend + لوحة الأدمن: **Node.js / NestJS + PostgreSQL**.
>
> كل قرار آخر أدناه هو اقتراح مبني على الـ 87 قسمًا في البرومبت، وقابل للنقاش قبل أي تنفيذ.

---

## Phase 1 — Architecture (المعمارية العامة)

### 1.1 المبدأ الأساسي
فصل تام بين 4 طبقات مستقلة، تتواصل فقط عبر API موثّق (REST + WebSocket)، بحيث يمكن استبدال أو إضافة أي طبقة دون إعادة بناء الباقي (متطلب القسم 22):

```
┌─────────────────┐     ┌──────────────────┐     ┌───────────────────┐
│  Android App     │     │  Admin Dashboard  │     │  (مستقبلًا)        │
│  Customer + Mode │     │  Web (React/Next) │     │  iOS / Web App     │
│  Merchant        │     │                    │     │  Merchant Standalone│
└────────┬─────────┘     └────────┬───────────┘     └─────────┬──────────┘
         │  HTTPS REST + WebSocket (Socket.IO)                │
         └──────────────────────┬───────────────────────────────┘
                                 │
                    ┌────────────────────────┐
                    │   NestJS API Gateway    │
                    │   (Auth, RBAC, Rate     │
                    │    Limiting, Validation)│
                    └────────────┬────────────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                         │
┌───────────────┐     ┌──────────────────┐      ┌────────────────────┐
│  Core Modules   │     │  Background Jobs  │      │  External Services  │
│ (Membership,    │     │ (BullMQ + Redis): │      │ - S3-compatible      │
│  Discounts,     │     │  QR rotation,     │      │   storage (صور)      │
│  Orders, Chat,  │     │  notifications,   │      │ - Push (FCM)         │
│  Affiliate...)  │     │  offline QR sync, │      │ - SMS/Email (OTP)    │
│                 │     │  reminders        │      │ - Payment Gateway    │
└───────┬─────────┘     └─────────┬─────────┘      │   (Abstraction)      │
        │                          │                └──────────┬───────────┘
        └──────────────┬───────────┘                           │
                        │                                       │
              ┌─────────────────────┐                           │
              │   PostgreSQL (RDS)   │◄──────────────────────────┘
              │   + Redis (Cache,    │
              │     QR tokens, Rate  │
              │     limiting)        │
              └─────────────────────┘
```

### 1.2 لماذا هذا الفصل؟
- **Mobile ≠ Backend ≠ Admin ≠ DB**: كل واحد Deployable و Scalable لوحده. الأدمن Dashboard مثلًا يمكن أن يكون تطبيق ويب منفصل تمامًا يستهلك نفس الـ API العام (بصلاحيات Admin/Staff).
- **Merchant App**: بدل بناء تطبيق Android منفصل الآن، سنبنيه كـ **Role-based module** داخل نفس تطبيق Android (نفس الـ APK)، مع فصل كامل على مستوى الشاشات والصلاحيات (قسم 23). البنية البرمجية (Clean Architecture) تسمح لاحقًا باستخراجه كتطبيق مستقل دون إعادة كتابة منطق الأعمال، لأن منطق الدومين (Domain Layer) سيكون معزولاً عن طبقة العرض.
- **API واحد لكل شيء**: نفس الـ NestJS backend يخدم Android + Admin Web + مستقبلًا iOS/Web، عبر REST موثّق بـ OpenAPI/Swagger، بدون أي منطق خاص بمنصة معينة داخل الـ Backend.

### 1.3 الأدوار (Roles) على مستوى النظام كله
كما في القسم 23 و55 و75:
- `CUSTOMER` — الزبون العادي.
- `MERCHANT_OWNER` — صاحب حساب التاجر الرئيسي.
- `MERCHANT_STAFF` *(مستقبلي، غير مطلوب بالمرحلة الأولى)* — موظف تاجر بصلاحيات محدودة (مثلاً موظف يقوم فقط بعمليات Scan).
- `AFFILIATE` — صفة إضافية فوق أي مستخدم (ليست دورًا حصريًا).
- `STAFF` — موظف منصة بصلاحيات RBAC محددة (قسم 55).
- `SUPER_ADMIN` — صلاحية كاملة.

**نقطة مهمة (قسم 75):** الدور ليس صفة ثابتة على المستخدم، بل المستخدم الواحد (`user_id` واحد) يمكن أن يملك `CustomerProfile` و`MerchantProfile` بنفس الوقت، ويبدّل بينهما من التطبيق. الـ JWT سيحمل قائمة الأدوار المتاحة للمستخدم + الدور النشط حاليًا (`active_context`).

---

## Phase 2 — Technology Stack

| الطبقة | التقنية | السبب |
|---|---|---|
| **Backend API** | Node.js 20 + NestJS (TypeScript) | معماري بالأساس (Modules/Providers/Guards)، RBAC ونظام Interceptors جاهز، دعم ممتاز لـ WebSocket وBullMQ، حسب قرارك المعتمد |
| **قاعدة البيانات** | PostgreSQL 16 | علاقات معقدة (Multi-vendor orders, Affiliate ledgers, Accounting) تحتاج RDBMS حقيقي وليس NoSQL |
| **ORM** | Prisma | Type-safety كامل بين الـ Schema والكود، Migrations واضحة، يتماشى ممتاز مع NestJS/TypeScript |
| **Cache / Realtime state** | Redis | تخزين QR tokens المتجددة كل 30 ثانية، Rate limiting، Pub/Sub لإشعارات الـ Chat اللحظية |
| **Realtime (Chat, Live Queue)** | Socket.IO (فوق NestJS Gateway) | Real-time فعلي لِـ Chat وقائمة الانتظار الحية (قسم 79) |
| **Background Jobs** | BullMQ (فوق Redis) | تدوير QR، تذكير المواعيد، مزامنة العمليات Offline، إشعارات مجدولة، انتهاء العروض المؤقتة تلقائيًا |
| **تخزين الملفات** | S3-compatible (Cloudflare R2 أو AWS S3) | صور المنتجات/المتاجر، مرفقات الدعم، فواتير PDF |
| **Push Notifications** | Firebase Cloud Messaging (FCM) | قياسي لتطبيقات Android، مجاني، يدعم Topics (لإشعارات المتابعين مثلاً) |
| **SMS / Email OTP** | مزوّد قابل للاستبدال عبر Interface (مثال: Twilio للـ SMS، Resend/SendGrid للبريد) | حسب القسم 56، يجب ألا يكون Hardcoded بمزود واحد |
| **Payment Gateway** | Abstraction/Interface فقط الآن (`PaymentProvider` interface) بمزودين فعليين: `ManualCashProvider` و`CodProvider` تعملان فعليًا من اليوم الأول، ومكان جاهز لإضافة `StripeProvider`/مزود محلي لاحقًا | القسم 24 و54 يمنعان صراحة أي Payment Gateway وهمي |
| **البحث** | PostgreSQL Full-Text Search + `pg_trgm` بالمرحلة الأولى، مع تصميم يسمح بالانتقال لاحقًا إلى Meilisearch/Typesense إذا كبر حجم البيانات | يكفي فعليًا لحجم الإطلاق الأول، ولا يبرر تعقيد بنية توزيعية إضافية له الآن |
| **الموقع الجغرافي** | PostGIS extension على PostgreSQL | حسابات المسافة (Nearby)، الفلترة بنطاق جغرافي (Radius) بشكل صحيح وسريع |
| **Admin Dashboard** | Next.js (React) + TypeScript، يستهلك نفس REST API | يفصل الواجهة عن الـ Backend بالكامل، ويسهل إعادة استخدام نفس المكوّنات لاحقًا في Web App للعملاء |
| **Android App** | Kotlin + Jetpack Compose، Clean Architecture (Data/Domain/Presentation) + MVVM، Hilt (DI)، Retrofit+OkHttp، Room (Local cache + Offline QR log)، DataStore (تفضيلات)، CameraX + ML Kit (لمسح QR من طرف التاجر) | التقنية الرسمية الحديثة لـ Android الحقيقي (ليس WebView أو Hybrid)، تدعم RTL/LTR بسهولة عبر نظام الـ Resources القياسي |
| **الترجمة (i18n)** | Backend: جداول Translation منفصلة لكل كيان قابل للترجمة (قسم 29). Android: `strings.xml` لكل لغة (`values-ar`, `values-de`, `values` كـ default إنجليزي) | مطابق تمامًا للقرار النهائي بالقسم 29 |
| **الاستضافة (مقترح مبدئي)** | Backend: أي مزود Docker-friendly (Render/Railway/Fly.io أو VPS + Docker Compose). DB: Managed Postgres (Supabase/RDS/Neon) | نفس نمط `express-print-saas` الحالي تقريبًا (استضافة مُدارة بسيطة)، تفاصيل نهائية في Phase 11 |

**ملاحظة صريحة:** لم يتم اختيار أي مزود دفع إلكتروني فعلي بعد لأن المستخدم لم يحدد واحدًا — هذا مطابق تمامًا لتعليمات القسم 24 ("لا تعمل Payment Gateway وهمي، اعمل Abstraction فقط").

---

## Phase 3 — Database Schema

المخطط الكامل موزّع على 13 **Domain** منطقي. كل جدول أدناه هو تصميم مفاهيمي (Conceptual) — الأعمدة الدقيقة وأنواع البيانات (DDL/Prisma schema) تُكتب في مرحلة التنفيذ الفعلي (Phase 4)، وليس هنا.

### 3.1 الهوية والمستخدمون (Identity)
- **User**: `id, phone, email, password_hash, primary_language, country, preferred_currency, birthday?, status[active/suspended/deleted], created_at`
- **CustomerProfile** (1:1 مع User): `user_id, display_name, avatar_url, privacy_settings(json)`
- **MerchantProfile** (1:1 مع User — صاحب التاجر): `user_id, business_name(base), primary_language, base_currency, approval_status[pending/approved/suspended], verified_badge, verification_method[manual|video_call]`
- **Address**: `user_id, label, lat, lng, city, country, is_default`
- **StaffAccount**: `user_id, created_by_super_admin_id, is_active`
- **Role / Permission / StaffRolePermission**: RBAC قياسي (قسم 55) — أدوار الموظفين مجموعات صلاحيات قابلة للتركيب، وليست Enum ثابت.
- **AuditLog**: `actor_user_id, actor_role, action, target_type, target_id, before(json), after(json), reason, created_at` — **جدول مركزي** تكتب فيه كل الأقسام الأخرى (36، 40، 53.1، 57، 58.2).

### 3.2 التجار والمتاجر (Merchant Domain)
- **Merchant** (الكيان القانوني/التجاري) `1—1` مع `MerchantProfile` أعلاه، أو نُدمجهما — القرار النهائي بالتنفيذ.
- **MerchantTranslation**: `merchant_id, lang, name, description`
- **Branch**: `merchant_id, lat, lng, address, phone, whatsapp, working_hours(json)` — فرع = نقطة فعلية على الخريطة (قسم 66).
- **BranchTranslation**
- **Category** (شجرة هرمية): `id, parent_id?, icon, sort_order`
- **CategoryTranslation**: `category_id, lang, name`
- **Store**: `merchant_id` (1:1) — الواجهة الإلكترونية للمتجر، منفصلة مفاهيميًا عن "الفروع الفعلية" (Branch) رغم أنها تتبع نفس التاجر.

### 3.3 المنتجات (Catalog)
- **Product**: `store_id, category_id, sku, base_price, currency, stock, status[active/out_of_stock/hidden], discount_enabled, discount_type[percent|fixed], discount_value, warranty_months?, moderation_status[pending/approved/rejected]`
- **ProductTranslation**: `product_id, lang, name, description`
- **ProductVariant**: `product_id, sku, attributes(json: size/color), price_override?, stock`
- **ProductImage**: `product_id, url, sort_order, moderation_status`
- **ProductPriceHistory**: `product_id, price, changed_at` — يغذّي تنبيه انخفاض السعر (قسم 83).

### 3.4 العضوية والحسم الرقمي (Membership & QR — جوهر المنتج)
- **MembershipPlan**: `name, price, currency, duration_days, benefits(json), discount_rules(json), is_family_plan, max_family_members, trial_days, status` — كل الأرقام هنا تُدار من `Setting` (قسم 60/61)، وليست Hardcoded.
- **Membership**: `user_id, plan_id, member_number(unique), status[trial|active|expired|cancelled], starts_at, ends_at, family_owner_membership_id?`
- **MembershipQrToken**: `membership_id, token_hash, expires_at(now+30s), created_at` — **لا يوجد QR ثابت مخزّن**؛ فقط آخر Token صالح، يُنشأ عند الطلب ويُتحقق منه Server-side (تفاصيل كاملة Phase 9).
- **MembershipBackupCode**: `membership_id, code_hash(12-16 خانة), expires_at, used_at?, used_offline` — بديل بدون إنترنت (قسم 62).
- **Discount**: `merchant_id, branch_id?, scope[all_products|specific_products|specific_category|service], type[percentage|fixed|limited_uses|free_item_every_n], value, usage_limit_per_customer?, usage_limit_total?, starts_at?, ends_at?, status`
- **DiscountTarget** (Many-to-Many): يربط `discount_id` بـ `product_id` أو `category_id` عند scope محدد.
- **DiscountUsage**: `discount_id, user_id, used_count` — العدّاد الذي يمنع تجاوز الحد لكل زبون (قسم 35)، Server-authoritative بالكامل.
- **DiscountTransaction**: `id, membership_id, merchant_id, branch_id, discount_id?, bill_amount, discount_percent, discount_amount, final_amount, status[confirmed|cancelled|failed], source[online_scan|offline_backup_code], transaction_ref(unique), created_at` — **مصدر الحقيقة الواحد** الذي يظهر مطابقًا عند الزبون/التاجر/الأدمن (قسم 34).

### 3.5 التجارة الإلكترونية (E-commerce — Amazon-style)
- **Cart / CartItem** (يمكن أن تكون Server-side أو Client-side متزامنة — القرار: Server-side لضمان تعدد الأجهزة وصحة السعر).
- **Order**: `customer_id, status, currency, payment_status, payment_method, created_at`
- **SubOrder**: `order_id, merchant_id, status[pending|confirmed|preparing|shipped|delivered|cancelled], shipping_fee, shipping_zone_id, tracking_ref`
- **OrderItem**: `sub_order_id, product_id, variant_id?, qty, unit_price, member_price_applied(bool), warranty_until?`
- **Payment**: `order_id? , membership_id?, provider[cash_manual|cod|online], amount, status[pending|paid|failed|refunded], confirmed_by_admin_id?`
- **ShippingZone**: `merchant_id, area_name, fee, eta_days, cod_allowed`
- **ReturnRequest**: `order_item_id, reason, status[requested|approved|rejected|admin_mediation], resolved_by`

### 3.6 التفاعل الاجتماعي (Social Layer)
- **Follow**: `user_id, merchant_id`
- **Favorite**: `user_id, target_type[merchant|product], target_id`
- **Like**: `user_id, target_type[comment|product|...], target_id`
- **Comment**: `target_type[merchant|product], target_id, user_id, body, image_url?, parent_comment_id?, is_merchant_reply, hidden_by_merchant, status[visible|reported|removed_by_admin]`
- **Review**: `target_type[merchant|product|branch], target_id, user_id, rating(1-5), verified_purchase(bool)`
- **Block**: `blocker_user_id, blocked_user_id, created_at`

### 3.7 الرسائل والإشعارات
- **Chat**: `customer_id, merchant_id, is_blocked`
- **Message**: `chat_id, sender_user_id, body, sent_at, seen_at?`
- **Notification**: `user_id, type, payload(json), channel[push|in_app], read_at?`
- **NotificationPreference**: `user_id, notification_type, in_app_enabled, push_enabled`
- **Device**: `user_id, push_token, platform`
- **SupportTicket / SupportTicketMessage**: منفصل تمامًا عن `Chat` (قسم 77).

### 3.8 النمو والتسويق (Growth)
- **LoyaltyLedger**: `user_id, delta, reason, ref_type, ref_id, balance_after`
- **Referral**: `referrer_user_id, referred_user_id, code, status[pending|rewarded]`
- **Affiliate**: `user_id, status[pending|approved|rejected], tier_id`
- **AffiliateTier**: `name, commission_percent`
- **AffiliateClick**: `affiliate_id, target_type, target_id, created_at`
- **AffiliateConversion**: `affiliate_id, click_id?, type[merchant_signup|customer_membership], amount, status`
- **AffiliateCommission**: `affiliate_id, conversion_id, amount, status[pending|approved|paid], paid_at?`
- **Banner**: `merchant_id, image_url, target_url, starts_at, ends_at, status[pending|scheduled|active|rejected|stopped], payment_id, rejection_reason?`
- **Boost**: `merchant_id, scope[category|featured], starts_at, ends_at, payment_id, status`
- **Collection / CollectionItem**: قوائم منسّقة من الأدمن (قسم 49).
- **GroupDeal / GroupDealParticipant**: (قسم 81).
- **GiftCard**: `code, initial_value, balance, currency, issuer_type[platform|merchant], issuer_merchant_id?, recipient_user_id?, message?, expires_at`

### 3.9 الحجوزات والخدمات (Bookings)
- **Service**: `merchant_id, name, duration_minutes, price`
- **AvailabilitySchedule**: `merchant_id/branch_id, weekday, start_time, end_time, slot_duration`
- **Booking**: `service_id, customer_id, starts_at, ends_at, status[pending|confirmed|cancelled|completed]`
- **QueueStatus**: `branch_id, waiting_count, avg_wait_minutes, updated_at` (قسم 79)

### 3.10 المحاسبة (Accounting)
- **Expense**: `category, amount, currency, date, description, attachment_url?, created_by, updated_by?`
- **ExpenseCategory**: قابلة للإضافة من الأدمن.
- الإيرادات (Revenue) **لا تُدخل يدويًا** — تُحسب كـ View/Query مجمّعة من: اشتراكات العضوية المدفوعة + البانرات المدفوعة + الـ Boosts (قسم 58.1 ينص أنها "تلقائية"). سيتم بناء `RevenueLedger` **كسجل مُشتق تلقائيًا** عند كل Payment ناجح مرتبط بمصدر دخل، بدل جدول يُدخل يدويًا، حتى يبقى دقيقًا (Single Source of Truth حسب مبدأ القسم 34 نفسه).

### 3.11 الإعدادات والامتثال
- **Setting**: `key, value, type, editable_by_role` — محرك التسعير المرن، الفترات التجريبية، نطاق الإشعار الجغرافي، حد أقصى الإشعارات اليومية... إلخ (كل شيء بالقسم 60/61 يُخزَّن هنا، صفر Hardcoding).
- **ExchangeRate**: `base_currency, target_currency, rate, updated_at`
- **Consent**: `user_id, type[gdpr_location|marketing|terms], granted_at, ip_hash`
- **DataDeletionRequest**: `user_id, status, requested_at, completed_at?`

### 3.12 العلاقات المفصلية (الأهم فهمًا)
- `User 1—1 CustomerProfile` و `User 1—1 MerchantProfile` (كلاهما اختياري، يمكن أن يجتمعا — قسم 75).
- `Merchant 1—N Branch`، و`Merchant 1—1 Store`، و`Store 1—N Product`.
- `Product 1—N ProductVariant`، `Product 1—N ProductTranslation`.
- `Membership 1—1 (current) MembershipQrToken` لكن تاريخيًا `1—N` (كل توكن قديم منتهي يُؤرشف أو يُحذف دوريًا).
- `DiscountTransaction N—1 Membership`, `N—1 Merchant`, `N—1 Branch`, `N—1 Discount(nullable)` — **هذا الجدول هو التقاطع الفعلي بين نظام العضوية ونظام التاجر**، وهو ما يُقرأ منه سجل الزبون + لوحة التاجر + لوحة الأدمن (نفس البيانات، Query مختلف فقط حسب صلاحية القارئ).
- `Order 1—N SubOrder 1—N OrderItem` (سلة متعددة التجار تُقسَّم تلقائيًا — قسم 59.5).
- `DiscountUsage` هو ما يمنع استخدام نفس الحسم مرتين بما يخالف حده (قسم 27 و35) — يُفحص Server-side **قبل** إنشاء أي `DiscountTransaction` جديد، ضمن Transaction واحدة في قاعدة البيانات (DB Transaction) لمنع Race Conditions عند مسح متزامن.

---

## Phase 4 — Backend Architecture (NestJS)

### 4.1 تقسيم الـ Modules (Domain-Driven)
```
src/
  modules/
    auth/              → JWT, OTP, RBAC Guards, Role/Permission
    users/             → CustomerProfile, MerchantProfile, Dual-role switch
    merchants/         → Merchant, Branch, approval workflow, moderation queue
    catalog/           → Category, Product, Variant, Image moderation
    membership/        → MembershipPlan, Membership, family plans
    qr/                → توليد/تدوير/تحقق QR + Backup Codes + Offline Sync
    discounts/         → Discount, DiscountUsage, DiscountTransaction
    orders/            → Cart, Order, SubOrder, ReturnRequest
    payments/          → PaymentProvider interface + ManualCash/COD providers
    chat/              → WebSocket Gateway, Message, seen/unseen
    notifications/     → Notification, Preferences, FCM dispatch
    social/            → Follow, Favorite, Like, Comment, Review, Block
    growth/            → Loyalty, Referral, Affiliate, Banner, Boost, GiftCard, GroupDeal
    bookings/          → Service, AvailabilitySchedule, Booking, QueueStatus
    accounting/        → Expense, RevenueLedger (derived), Reports
    admin/             → Staff RBAC, AuditLog queries, Dashboards/Statistics
    search/            → Full-text + Geo (Nearby/Radius)
    settings/          → Pricing engine, feature toggles
    compliance/        → Consent, DataDeletionRequest (GDPR)
  common/
    guards/ interceptors/ pipes/ filters/
    decorators/ (@Roles, @CurrentUser, @Permission)
  jobs/                → BullMQ processors (qr-rotation, reminders, offline-sync, expirations)
```

### 4.2 مبدأ الأمان (تطبيق مباشر للقسم 21)
- **لا ثقة بأي بيانات من التطبيق**: كل تحقق حساس (Membership status, QR token, Discount eligibility, Prices, Stock) يُعاد حسابه بالكامل من قاعدة البيانات داخل الـ Backend، ولا يُقبل أي مبلغ/نسبة/حالة يرسلها العميل كمُدخل نهائي — العميل يرسل فقط "النية" (مثلاً: bill_amount التاجر يكتبه، لكن نسبة الحسم تُقرأ من DB وليس مما يرسله التطبيق).
- **Guards متعددة الطبقات**: `JwtAuthGuard` → `RolesGuard` → `PermissionGuard` (للموظفين) → `OwnershipGuard` (مثلاً تاجر لا يعدّل منتج تاجر آخر).
- **Rate Limiting**: على مستوى Gateway (مثل `@nestjs/throttler`) خصوصًا على: Login, OTP request, QR scan endpoint.
- **Token Expiration**: JWT قصير العمر (Access ~15 دقيقة) + Refresh Token طويل، إبطال فوري للجلسات القديمة عند تغيير كلمة المرور أو حظر الحساب (قسم 56/57).
- **لا Secrets داخل تطبيق Android إطلاقًا** — كل مفتاح (Payment, FCM Server Key, إلخ) يبقى فقط على الـ Backend.

### 4.3 عملية التحقق من الحسم (الأهم في كامل النظام)
تسلسل فعلي (وليس Mock) لما يحدث عند مسح QR من التاجر:
1. التاجر يرسل `token` الممسوح + `merchant_id` (من الـ JWT الخاص به، وليس من الطلب).
2. Backend: يبحث عن `MembershipQrToken` غير منتهي مطابق للـ hash.
3. يتحقق: العضوية `active`، التاجر `approved`، الفرع يملك حسمًا فعالًا الآن، `DiscountUsage` لم يتجاوز الحد.
4. إذا كل شيء صحيح → يرجع للتاجر: اسم العضو (المسموح إظهاره حسب Privacy Toggle)، نسبة الحسم المطبّقة.
5. التاجر يُدخل `bill_amount` → Backend يحسب `discount_amount` و`final_amount` (وليس التطبيق).
6. عند "Confirm" → تُنشأ `DiscountTransaction` + تحديث `DiscountUsage` **داخل DB Transaction واحدة** (atomicity) لمنع استخدام مزدوج عند طلبين متزامنين لنفس العضوية.
7. QR Token المستخدم يُلغى فورًا (One-time-use ضمن التدوير الطبيعي كل 30 ثانية).

هذا التسلسل الكامل هو ما سنبنيه فعليًا بالتنفيذ — لا Mock validation إطلاقًا، تطبيقًا حرفيًا لقاعدة القسم 30.

---

## Phase 5 — Android Architecture

### 5.1 البنية (Clean Architecture + MVVM)
```
app/
  data/        → Retrofit APIs, Room (offline cache + offline QR log), Repositories (impl)
  domain/      → UseCases, Repository interfaces, Models (بدون أي اعتماد على Android/Retrofit)
  presentation/
    customer/  → Home, Search, Nearby, StoreProfile, Cart, Orders, MyCard(QR), Chat, Profile, Wallet, Social...
    merchant/  → MerchantHome, ScanMembership, ProductManagement, DiscountSetup, MerchantAnalytics, MerchantChat
    shared/    → Auth, RoleSwitcher, Notifications, Settings(Language/DarkMode)
  core/        → DI (Hilt), Networking, Navigation (Compose Navigation), Theme (Design Tokens من القسم 17)
```
- **Domain layer معزول تمامًا** عن Android Framework → هذا ما يجعل استخراج "Merchant App" كمشروع Android منفصل لاحقًا (قسم 23) مجرد إعادة تغليف لطبقة الـ Presentation، دون لمس منطق الأعمال.
- **RoleSwitcher**: عنصر تنقّل مركزي يبدّل شجرة الـ Navigation كاملة بين واجهة الزبون وواجهة التاجر (قسم 75)، مبني على `active_context` من التوكن.

### 5.2 التصميم (Design System) — تطبيق القسم 17/18
- Design tokens: `PrimaryRed #BA2A34`, gradient `#5A0E12→#BA2A34` (Hero/Splash/Membership Card فقط)، خلفية عامة بيضاء/فاتحة.
- Material 3 (Compose) مع Theme مخصص بالكامل، لا ألوان Material الافتراضية.
- `Corner radius` موحّد، `elevation` خفيف (Soft Shadows)، Bottom Navigation: **Home | Discover | Stores | Orders | Profile**.
- RTL/LTR: Compose يدعم `LayoutDirection` تلقائيًا حسب Locale — العربية RTL كاملة بدون أي عكس يدوي للعناصر.

### 5.3 كاميرا المسح (Merchant Scan)
- CameraX + ML Kit Barcode Scanning (يعمل محليًا بالكامل، بدون تبعيات خارجية مدفوعة).
- عند فشل الاتصال: واجهة إدخال يدوي لـ Backup Code (قسم 62)، تُسجَّل محليًا في Room كـ `PendingOfflineTransaction` وتُزامَن تلقائيًا عبر WorkManager عند عودة الشبكة.

---

## Phase 6 — Admin Dashboard

- تطبيق Next.js مستقل، RBAC واجهيًا (إخفاء أقسام لا يملك الموظف صلاحيتها) **+ تحقق فعلي مطابق على الـ Backend** (لا يكفي إخفاء الزر — القسم 55 يتطلب فرضًا حقيقيًا للصلاحية).
- الوحدات: Users, Merchants(Approval Queue), Categories, Products(Moderation Queue), Discounts/Offers, Orders, DiscountTransactions(سجل كامل قابل للفلترة/التصدير), Banners(Review Queue), Affiliate(Approvals+Payouts), Accounting(Expenses+Reports+Charts), Support Tickets, Staff & Permissions, Settings(محرك التسعير), Audit Logs.
- **الإحصائيات (قسم 15)**: Dashboard رئيسي بمؤشرات حيّة (مستعلمة مباشرة من DB، لا بيانات وهمية) + Charts (سنستخدم مكتبة Recharts).

---

## Phase 7 — Merchant System (ملخص وظيفي)

يغطي الأقسام 3، 4، 5، 20، 35، 45، 53، 59.3، 66، 67، 68، 79، 86 — التاجر يدير: الملف التجاري والفروع، المتجر الإلكتروني (منتجات/متغيرات/مخزون)، أنواع الحسم (بما فيها المحدودة بعدد الاستخدام)، شاشة Scan Membership، إحصائياته الخاصة، طلب Banner/Boost، مناطق الشحن، الحجوزات (إن كان نشاطه من نوع خدمي)، القائمة الرقمية QR، قائمة الانتظار الحية.

## Phase 8 — Customer System (ملخص وظيفي)

يغطي الأقسام 1، 6–13، 19، 31–34، 36–44، 46–51، 69، 70، 73–85، 87 — التسجيل، العضوية والبطاقة الرقمية (QR)، الاكتشاف حسب الموقع/التصنيف، صفحة التاجر والمنتجات، السلة والطلبات متعددة التجار، الشات، المتابعة/المفضلة/التعليقات/الإعجاب، المحفظة (نقاط + كوبونات + بطاقات هدايا)، الإحالة، مركز تفضيلات الإشعارات، الوضع الليلي، الفواتير الرقمية.

## Phase 9 — QR / Dynamic Membership System (تفصيل تقني نهائي)

آلية القسم 62 حرفيًا:
1. **Online**: كل 30 ثانية يطلب التطبيق (أو يُدفع له عبر WebSocket) Token جديد من `POST /membership/qr/rotate` → Backend يولّد Token عشوائي (256-bit)، يخزّن الـ hash فقط في Redis بـ TTL=35s (مع هامش أمان)، ويُبطل السابق فورًا. الـ QR المعروض = تشفير هذا الـ Token فقط، لا بيانات عضوية داخله إطلاقًا (فحص الحالة كله Server-side وقت المسح).
2. **Offline**: عند فقد الاتصال، التطبيق يعرض Backup Code (تم توليده وتخزينه محليًا مسبقًا في آخر اتصال ناجح، مع صلاحية قصيرة، ويُستهلك لمرة واحدة). التاجر يكتبه يدويًا في تطبيقه → إن كان هو أيضًا Offline، يُسجَّل محليًا بالوقت ويُرسَل تلقائيًا (WorkManager/BullMQ من الطرفين) عند عودة أي طرف للاتصال → Backend يتحقق بأثر رجعي، وإن فشل التحقق تُلغى العملية تلقائيًا مع إشعار للطرفين (Cancelled واضح، وليس اختفاء صامت).
3. **منع إعادة الاستخدام**: `MembershipBackupCode.used_at` يُقفل فور أول استخدام (حتى Offline محليًا)، ويُرفض أي محاولة ثانية بنفس الكود عند المزامنة.

## Phase 10 — Testing

- **Backend**: Jest (Unit) لكل UseCase/Service، خصوصًا منطق حساب الحسم ومنع الاستخدام المزدوج (Concurrency tests صريحة على `DiscountUsage`)؛ اختبارات Integration بقاعدة بيانات Postgres حقيقية (Testcontainers) للـ APIs الحرجة (QR verify, Checkout, Payment confirm).
- **Android**: JUnit + Turbine لاختبار ViewModels/UseCases، Compose UI Tests للشاشات الحرجة (My Card / Scan flow)، اختبار صريح لسيناريو Offline→Online sync.
- **E2E**: Playwright للـ Admin Dashboard، وسيناريو E2E كامل (Postman/Newman أو REST Client tests) لمسار: تسجيل زبون → اشتراك عضوية → مسح QR عند تاجر → تأكيد التحقق يظهر متطابقًا عند الزبون/التاجر/الأدمن.
- لا ميزة تُعتبر "جاهزة" في هذا المشروع دون أن تكون مرتبطة فعليًا بقاعدة بيانات حقيقية وتغطية اختبار للمسار الحرج — هذا شرط صريح من القسم 30.

## Phase 11 — Deployment

- Docker Compose محلي (Postgres + Redis + API) لتطوير مطابق للإنتاج.
- Backend: نشر كحاوية (Render/Railway/Fly.io أو VPS)، DB مُدارة (Supabase/Neon/RDS) مع نسخ احتياطي تلقائي.
- Migrations عبر Prisma Migrate ضمن خط CI/CD (GitHub Actions) قبل أي نشر.
- Android: توزيع أولي عبر Internal Testing Track على Google Play، ثم Closed/Open Testing، قبل الإصدار العام.
- **بيئات منفصلة إجباريًا**: `development` / `staging` / `production` — أي اختبار لميزة حساسة (دفع، خصم) يتم على `staging` بقاعدة بيانات منفصلة أولًا، لا اختبار مباشر على بيانات حقيقية.

---

## ما لم يُنفَّذ بعد (صراحة، حسب طلبك بعدم الإيهام)

هذه وثيقة **تصميم فقط** — لا سطر كود واحد كُتب بعد لهذا المشروع. كل ما سبق هو مقترح للمناقشة والتعديل. الخطوة التالية بعد موافقتك هي:
1. Scaffolding فعلي لهيكل مجلدات `dalilacom/backend` (NestJS) و`dalilacom/android` و`dalilacom/admin` (فارغة بالبداية، بدون منطق).
2. البدء بأول Feature فعلية متصلة بقاعدة بيانات حقيقية (Auth + Users على الأرجح، لأن كل شيء آخر يعتمد عليها)، واحدة في كل مرة، مع اختبار فعلي قبل الانتقال للتالية — تمامًا كما طلبت بـ"أسلوب العمل".

---

**بانتظار مراجعتك على هذه الوثيقة قبل أي Scaffolding فعلي.** أي تعديل على المعمارية أسهل بكثير الآن من بعد كتابة الكود.
