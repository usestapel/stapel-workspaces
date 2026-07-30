# Changelog

## [0.13.0] — 2026-07-30

Требования первого входа — независимые галки, и они доезжают до принятия
приглашения (#90).

`settings.security.provisioned_user_policy` был ОДНОЙ строкой, уезжал в
`auth.provision_user` как `first_login_policy`, а тот писал
`password_change_required=(policy == "password_change"),
mfa_enrollment_required=(policy == "mfa_enroll")` — назвать любое из
требований значило снять второе. Обе галки в модалке приглашения были
невыразимы нигде на этом пути. Они висели инертными именно поэтому, а не
потому, что кто-то забыл их подключить.

И это перестало быть декоративным: stapel-auth 0.15.0 перенёс гейт
первого входа внутрь `_issue_session_tokens` — единственного минтера, через
который проходят все 19 путей выдачи сессии, — с охватом «все пути» по
умолчанию. Поднятый флаг закрывает вход везде, а не только на форме пароля.

### Изменено

- **`settings.security.provisioned_user_policies`** — список вместо строки.
  `password_change` и `mfa_enroll` компонуются. Дообработка старой записи
  без миграции данных: `provisioned_user_policy` (строка) читается, когда
  множественного ключа нет; PATCH принимает обе орфографии. Мусорные
  элементы отбрасываются, а не роняют разбор — он выполняется на КАЖДОМ
  провижене и КАЖДОМ приёме приглашения, и опечатка в одном JSON-блобе не
  должна класть приглашения организации.
- `provision_member` шлёт `first_login_policies` (набор) вместо
  `first_login_policy`. **Требует stapel-auth >= 0.17** для набора; более
  старый auth ответит на неизвестный ключ структурной ошибкой, а не
  тихо — провижен упадёт видимо.

### Добавлено — политики доезжают до приёма приглашения

`accept_invitation` применяет требования организации к вступающему
аккаунту через `auth.apply_first_login_policies`. Два свойства этого шва
несущие:

- **шов не дёргается вовсе**, пока организация не настроила политики.
  Дефолт (`password_change`) существует ради аккаунтов, которые
  организация СОЗДАЛА: пароль там выбрал не владелец аккаунта, и он
  обязан перестать работать. Навязывать его человеку, вступившему со
  своей собственной учёткой, значило бы заставить менять пароль каждого
  нового сотрудника каждой организации, которая никогда не открывала
  экран безопасности. Плюс организация, ничего не настроившая, вообще не
  привязана этой строкой к версии auth;
- **когда политики настроены, а auth не может их поднять — приём
  приглашения падает** (503 / ключевая ошибка auth), и вся транзакция
  откатывается: ни членства, ни израсходованного приглашения, повтор
  после починки auth сработает. Организация назвала предусловие допуска;
  шов, который не может его исполнить, обязан отказать, а не впустить
  неукреплённого участника. Best-effort здесь — ровно та форма, которую
  принимает «контроль безопасности, который тихо перестал работать».

## [0.12.0] — 2026-07-30

Приглашение перестало быть письмом в никуда: админ видит, кто не принял,
и может отозвать или переотправить (#109).

Отправка инвайта была операцией «только на запись». Принятие рождало
строку участника — а всё остальное (письмо не дошло, в адресе опечатка,
человек отказался, инвайт молча протух) из продукта не было видно вообще.
Ростер отвечал на вопрос «кто внутри». На вопрос «кто до сих пор снаружи
и с какого числа» не отвечал никто, и подействовать на этот ответ было
нечем.

### Добавлено

- **`GET <ws>/invitations`** — список приглашений воркспейса,
  anchor-пагинация по `created_at`. Фильтр `?status=`:
  - `pending` (по умолчанию) — живые, действующие, занимающие место:
    ровно «кто ещё не принял»;
  - `never_accepted` — они же плюс отклонённые, отозванные и протухшие
    (аудиторский взгляд: «нет приглашения» и «человек отказался» — разные
    факты и разные следующие шаги);
  - `all` — вся история, включая принятые.

  Плюс `?search=` — подстрока по адресу. Токен приглашения в ответе
  отсутствует: это bearer-секрет, и список, который его несёт, раздаёт
  админу рабочую ссылку входа на каждый адрес, который он когда-либо
  приглашал.
- **`POST <ws>/invitations/<id>/revoke`** — отзыв. Терминальное «нет» со
  стороны организации, зеркало отказа приглашённого; в `status` они
  остаются различимыми навсегда. Занятое место возвращается организации в
  момент коммита — `pending()` считает места, и отозванная строка сразу
  перестаёт стоить денег.
- **`POST <ws>/invitations/<id>/resend`** — переотправка. **Токен
  ротируется**, TTL начинается заново, письмо уходит снова. Осознанно
  принимает ПРОТУХШЕЕ приглашение: сдохший TTL — это сбой доставки, а три
  сохранённых терминальных штампа — это решения. Ротация потому, что
  единственная защита токена — то, что он лежал в одном ящике, а
  переотправляют ровно тогда, когда неизвестно, где оказалась первая
  копия.
- Событие `workspace.invitation_revoked` (`workspace_id`, `invitation_id`,
  `role`, `revoked_by`) — единственная запись о том, КТО отозвал: в строке
  есть `revoked_at`, но нет `revoked_by`. Почта приглашённого в payload
  намеренно отсутствует — событие расходится всем подписчикам, а
  приглашение называет человека, который здесь ни на что не соглашался.

### Мандат и гонка

- Все три ручки гейтятся `members.invite`: мандат, который создаёт
  приглашения, он же ими и управляет. Отдельного права не заводилось —
  роль, которая может приглашать, но не может посмотреть собственные
  приглашения, это различие без применения.
- Отзыв и переотправка — **compare-and-set**, а не слепая запись: вью
  читает состояние, сервис перечитывает строку под `select_for_update()
  .unresolved()`. Между этими двумя чтениями и живёт конкурирующий
  accept; в 0.10.0 эту же гонку чинили с другой стороны (отзыв,
  закоммиченный между проверкой accept'а и его захватом строки,
  проигрывал). Теперь оба конца держит один предикат, и
  `tests/test_api_invitation_admin.py::TestRevokeIsCompareAndSet`
  прогоняет accept внутрь окна.
- Фильтры списка — канонические предикаты `InvitationQuerySet`
  (`pending()` / `never_accepted()`), а не новые написания.
  `test_lifecycle_predicates.py` запрещает рукописную копию колонок;
  `TestFiltersAreTheCanonicalPredicates` фиксирует вторую половину — что
  ответ ручки СОВПАДАЕТ с ответом предиката, а не просто избегает
  запрещённого написания.

### Изменено

- `InvitationResponse` дополнен полями `status` (производная метка),
  `declined_at` и `invited_by_id`. Аддитивно — существующий ответ
  `POST members/invite` не сломан.

## [0.11.0] — 2026-07-30

Каждая вью говорит в собственном исходнике, что можно гостю (#168).

`stapel-core` 0.16 превращает включённую ось `AUTH_ANONYMOUS` в вопрос,
на который этот модуль не отвечал. Гостевая сессия — полноценный
`is_authenticated`, поэтому голый `IsAuthenticated` её пропускает, а
исходник молчал о том, хотели этого или нет. Девять вью здесь молчали
(`stapel_core.adoption` W002 на живом деплое). Молчание убрано; сами
гейты в подавляющем большинстве не менялись — они уже стояли, просто
в теле метода, а не в заголовке класса.

### ⚠️ Гость больше не может создать воркспейс

`POST /workspaces/api/v1/` для анонимной сессии теперь **403** вместо 201.
У воркспейса есть владелец, и `create_workspace` делает им вызывающего;
для организации он же — биллинговый якорь. Анонимная учётка одноразовая
по построению: войти в неё повторно нельзя, и организация пережила бы
единственный аккаунт, который может ею управлять и за неё платить.
Существующий энтайтлмент-шов этого не ловил: без установленного биллинга
он деградирует в «разрешить», а личный воркспейс он не гейтит никогда.

Это единственное поведенческое изменение в релизе — отсюда минор, а не
патч.

### Гостевая поверхность, названная явно

- **`WorkspaceListCreateView` (GET) — на живом гостевом пути**, остаётся
  открытой: шапка приложения спрашивает «в каких воркспейсах я состою» для
  ЛЮБОЙ сессии, гостевой в том числе, чтобы решить, что рисовать. Ответ
  гостю — пустой список: это правда, и это дешевле, чем 403, который шапке
  пришлось бы разбирать отдельной веткой. `ANONYMOUS_ALLOWED`.
- **`RoleListView`** — метаданные деплоя (реестр ролей для `RoleSelect`), не
  чьи-либо данные. Закрывать нечего, а сломать фронт можно.
  `ANONYMOUS_ALLOWED`.
- **`WorkspaceDetailView`, `MemberListView`, `MemberInviteView`,
  `MemberProvisionView`, `MemberDetailView`** — уже закрыты `_capability_check`
  в теле: у гостя нет строки `WorkspaceMember`, значит `membership is None`
  и 403 `forbidden_workspace` до любого обращения к данным.
  `ANONYMOUS_DENIED` — декларация не добавляет гейт, она делает
  существующий читаемым из заголовка класса.
- **`InvitationAcceptView`, `InvitationDeclineView`** — закрыты совпадением
  почты: приглашение персональное, а у анонимной учётки почты нет вовсе
  (подтверждение почты — ровно тот акт, который снимает `is_anonymous`).
  `ANONYMOUS_DENIED`.

Новый `tests/test_guest_surface.py` фиксирует обе половины: что гостю
по-прежнему доступно (список воркспейсов, реестр ролей) и что недоступно.

### Изменено

- Минимальный `stapel-core` поднят до `>=0.16` (релиз с
  `ANONYMOUS_ALLOWED`/`ANONYMOUS_DENIED`).

## [0.10.0] — 2026-07-30

Один канонический предикат жизненного цикла вместо девяти рукописных
написаний (#132).

### ⚠️ Меняет суммы счетов: отклонённый инвайт больше не держит место

Расчёт мест (`member_seats_quantity`, энфорсмент `workspaces.members.max`)
считал «живыми» приглашения, у которых не проставлены `accepted_at` и
`revoked_at` и не истёк TTL. Про `declined_at` — колонку, появившуюся
вместе с флоу отказа в 0.7.0, — это написание не знало. То есть
приглашение, от которого приглашённый **явно отказался**, продолжало
резервировать платное место до самого истечения TTL (по умолчанию 7 дней).

Теперь живое приглашение = `InvitationQuerySet.pending()`, ровно
`InvitationStatus.PENDING`: ни одной терминальной отметки и TTL не истёк.

**Что это значит на практике.** У организаций с отказами расчётное число
мест падает; проверка энтайтлмента начнёт пропускать приглашения, которые
раньше упирались в потолок. Это ровно тот же класс расхождения, что
чинили в 0.9.0 для приостановленных участников, только на другой половине
формулы — и ровно поэтому оба написания больше не пишутся руками.

Что при этом **не** изменилось: живое приглашение по-прежнему резервирует
место, хотя приглашённый ещё ни разу не заходил. Это осознанно (всплеск
приглашений не должен перепрыгнуть план между отправкой и принятием) — и
это то место, где формула модуля расходится с формулой владельца
(«зарегистрирован И активирован в этом месяце; ни разу не заходившие не
считаются»). Ни один тест в репозитории эту сверку не делает.

### Изменения поведения

- **`member_count` в ответе воркспейса больше не считает приостановленных.**
  Единственное место, где приостановленное членство ещё считалось хоть
  где-то: доступ, comm-функции, внутренний API и счёт мест исключают его,
  а карточка воркспейса показывала. Организация из 5 «участников» при
  оплате за 4 — ровно то расхождение, ради которого предикат сводится в
  одно место. Потребителям фронта: число может уменьшиться без каких-либо
  действий с членствами.
- **Отозванное приглашение больше нельзя принять в гонке.** Блокировка
  строки в `accept_invitation` (`select_for_update`) сверяла `accepted_at`
  и `declined_at`, но не `revoked_at`, — хотя у `decline_invitation`
  соседняя блокировка сверяла все три. Отзыв, закоммиченный между
  проверкой состояния во вьюхе и захватом строки, проигрывал гонку, и
  приглашение принималось. Теперь у обоих переходов один и тот же
  compare-and-set (`InvitationQuerySet.unresolved()`).
- **GDPR-удаление названо честно.** `WorkspacesGDPRProvider.delete()`
  удаляет отправленные пользователем приглашения, которые никогда не стали
  членством (`never_accepted()`), — отклонённые, отозванные и истёкшие в
  том числе. Поведение то же, что и было; изменилось только имя предиката:
  «никогда не принято» ≠ «живое», и стирание спрашивает именно первое.

### Added

- **`models.MembershipQuerySet`** — единственное место, где вообще
  пишутся колонки жизненного цикла членства:
  - `.active()` — «может действовать» (принят И не приостановлен):
    comm-функции, внутренний эндпоинт, `permissions.get_membership`,
    собственный список воркспейсов, свипы приостановки, `member_count`;
  - `.accepted()` — принят, **приостановленные включены**. Нарочно НЕ
    авторизационный предикат: только для поверхностей, которым надо
    честно показать приостановленную строку (403
    `membership_suspended` вместо голого «не участник»);
  - `.suspended(reason=…)` — каждый консюмер снимает только свою причину
    (MFA не снимает `account_deactivated` и наоборот);
  - `.holds_seat()` — «занимает платное место». Отдельное имя при тех же
    строках, что у `.active()`, — нарочно: «может действовать» и «стоит
    денег» это разные вопросы с сегодня совпадающим ответом, и пока они
    делили рукописное написание, ответы разъехались (#92). Кто их
    разведёт — правит это тело и пишет там почему.
- **`models.InvitationQuerySet`** — то же для приглашений: `.pending()`,
  `.unresolved()` (без часов — цель compare-and-set у accept/decline),
  `.accepted()`, `.never_accepted()`.
- **`tests/test_lifecycle_predicates.py`** — запирающий тест: набор
  краснеет с именем файла и строки, если сырое `accepted_at__isnull` /
  `suspended_at__isnull` / `declined_at__isnull` / `revoked_at__isnull`
  появится в запросе где-либо, кроме `models.py`. Плюс таблица «место →
  предикат» — по тесту на каждое из бывших написаний.

### Чего этот тест НЕ ловит

Соответствует ли `active()` формуле владельца, не знает ни один тест.
Правило предотвращает только **повторный** дрейф — тот, где копии
перестают соглашаться друг с другом, — после того как человек однажды
перевёл спеку в колонки. Первый, смысловой разъезд ловит только
спеко-производный тест или сквозной сценарий, и ни того ни другого здесь
нет. Конкретно: `last_accessed_at` не участвует ни в одном предикате, а
живой инвайт держит место за того, кто ни разу не заходил.

### Что не тронуто (найдено, оставлено)

- **Два из «девяти написаний» предикатом членства не были**: `gdpr.py`
  фильтрует ПРИГЛАШЕНИЯ (та же колонка `accepted_at`, другая модель,
  другой вопрос). Схлопывание их в `active()` было бы тихой сменой
  поведения — они получили предикаты приглашений.
- **Написаний было не девять, а пятнадцать** (29 сырых kwarg'ов):
  0.9.0 добавил десятое (`suspend_memberships_for_deactivated_user`),
  плюс лифты по причине и compare-and-set'ы приглашений.
- **`Workspace.deleted_at`** под правило не заведён: мягкое удаление —
  одна колонка, а разъезжаться нечему, пока формула не составная.
  Фильтров `deleted_at__isnull=True` в модуле хватает; если появится
  вторая колонка жизненного цикла воркспейса, их надо свести так же.

## [0.9.0] — 2026-07-30

Воркспейсы узнают о деактивации аккаунта (#92). Пара к stapel-auth 0.16
(`user.deactivated` / `user.reactivated`).

### ⚠️ Меняет суммы счетов: приостановленный участник больше не занимает место

`member_seats_quantity()` (энфорсмент энтайтлмента `workspaces.members.max`)
считал места как «принятые членства + живые инвайты», включая
**приостановленных**. Приостановка — это ровно «членство перестаёт
считаться везде»: все пути доступа (`permissions.get_membership`,
`has_capability`, comm-функции, внутренний API) уже фильтруют по
`suspended_at IS NULL`. То есть счёт выставлялся за людей, которых продукт
не пускает, — расхождение с собственной формулой модуля.

Теперь места = **активные** (принятые и не приостановленные) участники +
живые инвайты. Инвайты по-прежнему резервируют место, чтобы всплеск
приглашений не перепрыгнул план между отправкой и принятием.

**Что это значит на практике.** Для организаций с приостановленными
участниками расчётное число мест падает, и проверка энтайтлмента начнёт
пропускать приглашения, которые раньше упирались в потолок. Расхождение
было незаметным, пока приостановки были редкостью (только политика
`no_mfa`); деактивация аккаунтов делает их рутиной, а администратор,
деактивирующий уволившегося, обязан освободить место, а не только логин.
Место возвращается на `user.reactivated` — в этом и смысл обратимого
состояния.

### Added

- **Потребитель `user.deactivated`** (`actions.handle_user_deactivated` →
  `services.suspend_memberships_for_deactivated_user`) — приостанавливает
  **все** принятые членства аккаунта с причиной `account_deactivated`
  (`models.SUSPENSION_ACCOUNT_DEACTIVATED`). В отличие от MFA-потребителя
  это не зависит от политики воркспейса: никакая настройка безопасности не
  делает деактивированный аккаунт допустимым. Мягко удалённые воркспейсы
  пропускаются (отзывать там нечего). Механизм — тот же самый
  `suspend_member` (`suspended_at` + причина, эмит
  `workspace.member_suspended`, сброс кэша членства), а не второй способ
  выключить членство; письма подавлены (`notify=False`): аккаунт только что
  потерял все входы, а формулировка письма всё равно MFA-шная.
- **Потребитель `user.reactivated`** (`actions.handle_user_reactivated` →
  `services.lift_deactivation_suspensions_for_user`) — снимает **только**
  приостановки `account_deactivated`. Приостановка `no_mfa` принадлежит
  MFA-потребителю и остаётся: иначе восстановление аккаунта тихо провело бы
  пользователя без второго фактора обратно в require_mfa-воркспейс. Без
  этого обработчика деактивация была бы ловушкой — гвард сессий снова
  пускает, а продукт пустой.
- **`models.MemberState`** — производный (никогда не хранимый) словарь
  жизненного цикла членства: `invited` / `active` / `suspended` /
  `deleted`, плюс свойство `WorkspaceMember.state`. Заведён именно затем,
  чтобы «приостановлен» и «удалён» нельзя было прочитать одинаково с двух
  nullable-колонок на глаз. `deleted` из свойства недостижим — это
  отсутствие строки.
- JSON Schema потребляемых событий: `schemas/consumes/user.deactivated.json`,
  `schemas/consumes/user.reactivated.json`.

### `user.deleted` (GDPR) остаётся отдельным путём

Административная деактивация обратима и обязана оставить приостановленное
членство, в которое можно вернуться; GDPR-стирание (`user.deleted` →
`WorkspacesGDPRProvider.delete`) удаляет строки и мягко удаляет
принадлежащие аккаунту воркспейсы. Ни один из путей не подменяет другой, и
деактивация не трогает воркспейсы, которыми аккаунт владеет.

### Идемпотентность

Доставка событий at-least-once, поэтому повторная доставка
`user.deactivated` не падает, ничего не эмитит второй раз и **не
перезатирает `suspended_at` первой приостановки**. Членство, уже
приостановленное за `no_mfa`, сохраняет свою причину — и остаётся
приостановленным после восстановления аккаунта, что верно: пробел в MFA не
исчез оттого, что аккаунт вернули.

## [0.8.1] — 2026-07-26

### Added — `error-keys/` is finally mounted

`WorkspacesErrorKeysView` has existed since the port but no `urls*.py` ever mounted it — in
*any* stapel library. stapel-translate's `error_collector` polls
`/{prefix}/api/v1/error-keys/` on every service, so the whole endpoint class
answered 404 from Django's URL resolver and the collector harvested nothing
while reporting a plain `HTTP 404`. It is now mounted in `urls_v1.py` at
`error-keys/` (v1 canon), service/staff-gated as the base view declares.

Deliberately **not** in the contract triad: `ErrorKeysView` sets
`schema = None` and `/error-keys` is on the flows allowlist, so `make
contract` is a no-op diff — this is infrastructure, not product surface.

## [0.8.0] — 2026-07-24

Wave 3 of the workspaces org-program (spec §C1-C3): the workspaces side of
the security harden — org-provisioned (synthetic) users and the
`require_mfa` policy implemented as suspension-not-removal. Pairs with
stapel-auth 0.12 (`auth.provision_user` / `auth.mfa_status` /
`user.mfa_enabled|disabled` emits), stapel-billing 0.5 (`billing.debit`),
stapel-core 0.14 (`requires_verification` step-up) and
stapel-notifications 0.4 (`workspace.provisioned_account` /
`workspace.mfa_suspension` / `workspace.mfa_restored` types).

### Added
- **`POST <workspace_id>/members/provision`** — org-created login/password
  accounts (spec §C1). Full username is
  `{workspace_slug}/{username_local}` (slug is globally unique — orgs
  cannot collide); the account is created by `auth.provision_user` with
  the workspace's first-login policy
  (`settings.security.provisioned_user_policy`:
  `password_change` (default) | `mfa_enroll`) and joins immediately
  (`WorkspaceMember(accepted_at=now, provisioned=True)`, migration
  `0004`). Gate stack in order: HIGH step-up
  (`@requires_verification(scope="sensitive")` — same store as admin
  step-up) → capability `members.provision` (403) → entitlement
  `workspaces.provision_user` (402, degrade-allow without billing) →
  optional `billing.debit` when
  `STAPEL_WORKSPACES["PROVISION_USER_CREDITS"]` > 0 (deterministic
  idempotency key `ws-provision:<uuid>` per provision attempt; refused
  debit → 402). Response `{user_id, username, role, generated_password?}`.
- **Credentials delivery (the email nuance)**: a synthetic account
  normally has NO email — with `email` omitted the letter is skipped and
  the server-generated password is returned in the API response to the
  admin exactly once. With the optional `email` passed (stored UNVERIFIED
  by auth), the `workspace.provisioned_account` letter also carries the
  credentials (`username`, `initial_password` only when generated,
  `login_url`). The generated password never rides any event payload and
  is never logged.
- **Provision errors**: auth's structured failures pass through keyed
  with the status taken from the key (`error.409.username_taken`,
  `error.400.username_namespace_invalid`, `error.400.bad_request`); a
  malformed local part fails fast (before any debit/auth roundtrip) with
  the new `error.400.invalid_provision_username`; auth not wired → honest
  503 `error.503.auth_unavailable` (never degrades to allow).
- **Suspension (spec §C3)**: `WorkspaceMember.suspended_at` +
  `suspension_reason` (migration `0004`, expand-only; canonical reason
  `no_mfa`). Suspension is NOT removal — the row and role stay, but every
  access surface stops counting the membership:
  `permissions.get_membership`/`has_capability`/`require_capability`,
  `workspaces.check_membership`/`check_capability` comm Functions, the
  internal HTTP membership endpoint, and the member's own workspace list.
  The view layer answers the honest 403
  `error.403.membership_suspended {reason}` (membership fetched with
  `include_suspended=True`); the cross-service membership cache is
  invalidated on suspend/unsuspend. `services.suspend_member` /
  `unsuspend_member` are idempotent and emit
  `workspace.member_suspended` / `workspace.member_unsuspended`
  (+ schemas) inside the transactional outbox.
- **`WorkspaceSecuritySettings`** — typed dataclass over
  `Workspace.settings["security"]` (`require_mfa`,
  `provisioned_user_policy`; extra keys pass through for client
  extension). A PATCH whose settings payload carries the `security` block
  additionally gates on capability `workspace.security.manage` + HIGH
  step-up (delegate-method pattern — ordinary PATCHes stay step-up-free)
  and validates the two known keys.
- **require_mfa sweep**: flipping `require_mfa` ON runs a synchronous
  `auth.mfa_status` pass over active members; no strong factor → suspend
  (reason `no_mfa`, emit + `workspace.mfa_suspension` letter). Auth
  unavailable during the pass → members are NOT touched (fail-open by
  suspension — fail-closed would lock out the whole org); the policy
  still saves and the event consumer catches up. Flipping it OFF lifts
  the `no_mfa` suspensions it caused (emit, no letter — the mfa_restored
  wording is about the user enabling 2FA, wrong for a policy drop).
- **MFA event consumers** (`actions.py`, idempotent): `user.mfa_disabled`
  → suspend the user's memberships in every `require_mfa` workspace
  (reason `no_mfa`, emit + mfa_suspension letter); `user.mfa_enabled` →
  lift the user's `no_mfa` suspensions ONLY (other reasons are not MFA's
  to lift; emit + mfa_restored letter). Consumed schemas mirrored in
  `schemas/consumes/`.
- **Member surface**: `MemberResponse` gains additive `provisioned`,
  `suspended_at`, `suspension_reason` (members list shows suspension
  state); `workspace.member_provisioned` emit
  `{workspace_id, user_id, role, provisioned_by}` (+ schema); admin list
  shows/filter the new member state; entitlement seam gains
  `ENT_PROVISION_USER` + `debit_provision_credits`.
- Errors + ru: `error.403.membership_suspended` (`{reason}`, remediation
  `fix_input` — the canonical reason is self-serve and restores
  automatically), `error.400.invalid_provision_username` (`fix_input`).
  Contract triad + capabilities.json + error docs regenerated.

## [0.7.0] — 2026-07-24

Wave 2 of the workspaces org-program (spec §B1-B3): the workspaces side of
the invite flow. Pairs with stapel-auth 0.11 (`auth.issue_login_grant` /
`POST /grant/exchange/`); the frontend flow machine lives at the canonical
`/invite/{token}` route.

### Added
- **Invitation state machine**: `WorkspaceInvitation.declined_at`
  (migration `0003`, expand-only) — decline is the invitee's terminal "no",
  distinct from the workspace's `revoked_at`; derived
  `WorkspaceInvitation.status` property (`InvitationStatus`:
  `pending | accepted | declined | revoked | expired`, stored terminal
  timestamps beat the TTL). Accept/decline/claim share one 400 mapping with
  the same precedence.
- **`GET invitations/<token>`** — AllowAny public preview for the
  `/invite/{token}` page: `{workspace_name, role, email_masked
  (m***@d***.com-style), status, email_registered, expires_at}`.
  `email_registered` (case-insensitive account lookup) steers the frontend
  to login vs claim. Throttled (`ScopedRateThrottle`, scope
  `workspace-invitation`, rate from
  `STAPEL_WORKSPACES["INVITATION_THROTTLE"]`, default `30/min`, `None`
  disables) as an enumeration backstop.
- **`POST invitations/<token>/decline`** — authenticated + email-match
  (personal in both directions, like accept); sets `declined_at`
  (row-locked); a later accept answers 400 `error.400.invitation_declined`.
- **`POST invitations/<token>/claim`** — AllowAny, for
  `email_registered == false` only: valid pending invite →
  `auth.issue_login_grant` `{email, verified_email: true, create_if_missing:
  true, language?}` (Accept-Language hint) → `{grant_token}`. Registered
  email → 409 `error.409.email_already_registered`; auth Function not wired
  → honest 503 `error.503.auth_unavailable` (an invite flow without auth is
  meaningless — this seam never degrades to allow, unlike billing's). The
  invitation is NOT consumed — accept stays a separate deliberate step.
- **Token hygiene**: the invite-flow endpoints carry the bearer token in
  the URL path, so `TokenPathNoLogMixin` suppresses Django's 4xx/5xx
  `request.path` log line (documented `_has_been_logged` contract) — the
  token never reaches the logs from module or framework code (gated by
  test).
- **Settings**: `STAPEL_WORKSPACES["INVITATION_THROTTLE"]` (default
  `"30/min"`).
- **Errors** (+ru, i18n gates green): `error.400.invitation_declined`
  (contact_support), `error.409.email_already_registered` (reauthenticate),
  `error.503.auth_unavailable` (wait_and_retry).
- Public API exports: `decline_invitation`, `issue_invitation_login_grant`
  (services), `ISSUE_LOGIN_GRANT` Function name constant on the module.

### Changed
- **Invite email links the canonical frontend route**
  `{FRONTEND_URL}/invite/{token}` (was `/invitations/{token}/accept`) —
  the pair's `InviteAcceptFlow` mount point (spec §B1).
- `accept_invitation` / accept view honour `declined_at` (row-lock filter +
  400 mapping); contract triad regenerated (3 new operations, 2 schemas,
  3 error keys).

## [0.6.0] — 2026-07-24

Wave 1a of the workspaces org-program (spec §A mandate model + §D2
entitlement seam). Pre-1.0 minor = breaking allowed; the backward
compatibility of the builtin four roles (viewer < member < admin < owner,
same capabilities as the old role thresholds) is the release gate and is
covered by tests.

### Added
- **Settings namespace** `STAPEL_WORKSPACES` (`conf.py`): `ROLES` (role
  registry overlay), `CAPABILITY_LEVELS` (step-up level overlay),
  `INVITATION_TTL_DAYS` (default 7 — the previously hard-coded invite
  expiry), `PROVISION_USER_CREDITS` (used from W3).
- **Mandate model** (`capabilities.py`): `BUILTIN_ROLES` (owner `*` rank
  400 / admin 300 / member 200 / viewer 100 per spec §A1),
  `effective_roles()` (last-wins merge of the `ROLES` overlay; `owner`
  system-protected), wildcard capability matcher (`*`, `prefix.*`),
  `capabilities_for()` / `role_has_capability()` / `role_rank()`,
  `BUILTIN_CAPABILITY_LEVELS` + `capability_level()`. System checks
  (`checks.py`, E001-E008) validate the overlays at startup.
- **Permission layer** (`permissions.py`): `has_capability()` /
  `require_capability()` (accepted memberships only; suspension arrives in
  W3). `role_at_least()` moved from list-index to registry ranks —
  identical answers for the builtin four (gate), custom roles participate
  via `rank`. `ROLE_HIERARCHY` kept as export.
- **comm**: new Function `workspaces.check_capability`
  `{workspace_id, user_id, capability}` → `{allowed, role}` (+ schema);
  `workspaces.check_membership` response now carries `capabilities: [str]`
  (additive; raw grant strings, wildcards included) — pairs with the
  stapel-core 0.14 consumer helper `require_capability()`.
- **HTTP API**: `GET /workspaces/api/v1/roles` (authenticated) → the
  effective registry `[{role, rank, capabilities, builtin}]`, rank-desc;
  `WorkspaceResponse.my_capabilities` (additive) on list/detail.
- **Member lifecycle emits** (spec §A4, transactional outbox, schemas in
  `schemas/emits/`): `workspace.member_removed` `{workspace_id, user_id,
  role, removed_by}` and `workspace.member_role_changed` `{workspace_id,
  user_id, old_role, new_role, capabilities}` from member DELETE/PATCH.
- **Entitlement seam** (`entitlements.py`, spec §D2):
  `check_org_entitlement()` / `check_entitlement()` call
  `billing.check_entitlement` (owner-anchored) and degrade to ALLOW on
  `FunctionNotRegistered` / `FunctionRouteNotConfigured` (billing not
  installed — OSS default); a failing installed billing propagates.
  Enforcement: work-workspace creation → `workspaces.org` (402
  `error.402.entitlement_required`); invite + accept →
  `workspaces.members.max` with seats = accepted + live pending invites
  (+ batch size on invite; re-checked on accept) → 402
  `error.402.member_limit_reached {limit}`.
- **Errors** (+ru, i18n gates green): `error.403.missing_capability
  {capability}` (member whose role lacks the capability; not-a-member
  keeps `error.403.forbidden_workspace`), `error.402.entitlement_required`,
  `error.402.member_limit_reached {limit}`.
- Public API exports: capability/entitlement helpers, `CHECK_CAPABILITY`,
  new event names.

### Changed
- **Views enforce capabilities instead of role thresholds** (same outcomes
  for the builtin four): workspace GET → `workspace.view`, PATCH →
  `workspace.update`, member list → `members.view`, invite →
  `members.invite`, role change → `members.role.change`, removal →
  `members.remove`. Owner-only invariants stay hardcoded on the `owner`
  role: workspace delete, granting/changing owner, last-owner protection.
- **Migration 0002** (expand-only): `WorkspaceMember.role` /
  `WorkspaceInvitation.role` `CharField(16)` → `CharField(32)`. Model
  `choices` stay on the builtin four (stapel-recordings `SourceType`
  precedent); serializers validate against `effective_roles()` — custom
  registry roles are invitable/assignable, granting `owner` via invitation
  remains forbidden.
- Invitation expiry reads `STAPEL_WORKSPACES["INVITATION_TTL_DAYS"]`.
- `schemas/emits/workspace.member_joined.json`: `role` enum of the builtin
  four widened to plain string (registry roles join via invitations).
- `stapel-core` floor `>=0.10` → `>=0.14` (comm exceptions contract +
  consumer-side `require_capability` counterpart).

### Notes
- Breaking surface (pre-1.0 minor): member-but-no-capability 403 payloads
  now carry `error.403.missing_capability` (was
  `error.403.forbidden_workspace`); `check_membership` gained a response
  field (additive).
- The monolith aggregate (`stapel-example-monolith`) needs its usual
  post-release `codegen` regen — the byte-identity test compares against
  the released 0.5.4 slice until then.

## [0.5.3] — 2026-07-17

Fix-up #2: 0.5.2's regen still baked the old version into
`docs/capabilities.json` (`make contract` ran before the version bump
landed). Re-ran with 0.5.3 already in `pyproject.toml`; verified match,
suite green.

## [0.5.2] — 2026-07-17

Fix-up: 0.5.1's CI/publish failed on contract drift — `docs/capabilities.json`
embeds the package version and wasn't regenerated for the 0.5.1 bump.
Regenerated via `make contract`; no other diff.

## [0.5.1] — 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.5.0] — 2026-07-17

### Removed
- **Legacy Kafka-topic remnants swept** (breaking → minor per house law):
  - `events.TOPIC_WORKSPACE_PERSONAL_CREATED` — alias for the retired Kafka
    topic `stapel.workspaces.personal-created`; no importers anywhere in the
    workspace. Use `EVENT_WORKSPACE_PERSONAL_CREATED`.
  - The duplicate `EVENT_REGISTRY` entry keyed by that alias (registry keeps
    the single canonical `workspace.personal.created` entry).
  - `STAPEL_TOPIC_USER_REGISTERED` env override in `consume_auth_events` —
    existed only for legacy topic layouts; on the bus transport the topic is
    always the action name (`user.registered`), now hard-coded. MODULE.md
    config table and override guidance updated.

## [0.4.4] — 2026-07-17

### Fixed
- `docs/capabilities.json` regenerated again — 0.4.3's release commit ran
  `make contract` before the version bump landed, so the committed file
  still baked in `0.4.2` (`test_capabilities_envelope` caught it in the
  0.4.3 publish retry).

## [0.4.3] — 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules).
- `docs/schema.json` regenerated against core 0.11.2 — error object gained
  `error_language` field and a reworded `error` description; no drift
  otherwise.

## [0.4.2] — 2026-07-16

### Changed
- **v1 canon sweep §60** (api-versioning.md §2, §6): `urls.py` renamed to
  `urls_v1.py` (paths inside unchanged); the new root `urls.py` mounts it
  under `v1/` and re-exports `GATE_REGISTRY`. Hosts including
  `stapel_workspaces.urls` under `workspaces/api/` now serve
  `/workspaces/api/v1/...`; bare paths no longer exist (sweep lands before
  the §3 API00x gates are enabled).
- Contract artifacts regenerated (`make contract`): `/v1/` in schema paths.
- `_capabilities.py` canonical_prefix → `/workspaces/api/v1`.
- Lint hygiene to a clean `stapel-verify`: explicit `# noqa` on pre-existing
  findings (R002/R004/R006/R007, CFG001).

### Added — per-module contract emission: `schema` + `flows` triad (contract-pipeline.md Wave 1)

stapel-workspaces now emits its **own** API contract per-module, completing the
triad `docs/{schema,flows,errors}.json` (`errors.json` already existed). The
frontend codegen can now read workspaces' committed artifacts instead of the
monolith aggregate at floating `main` — contract-pipeline.md verdict **A**
(contract = a reviewable, version-pinned commit). Copies the stapel-auth ETALON
harness (`_codegen_settings.py` / `codegen_urls.py` / `_codegen.py` / `Makefile`
/ `tests/test_contract.py`), adapted for this module's shape.

- **Harness** (reuses `stapel_tools.codegen`, adds ~90 lines of per-module config):
  - `_codegen_settings.py` — single source of truth for the `settings.configure`
    block, shared with `conftest.py` (extracted, no test-behavior change); a
    `contract=True` mode swaps in the production `REST_FRAMEWORK`.
  - `codegen_urls.py` — mounts `stapel_workspaces.urls` alone at the canonical
    `workspaces/api/` prefix (exactly as the monolith does — no sibling is
    co-mounted under this prefix, unlike auth+gdpr).
  - `_codegen.py` — the `python -m stapel_workspaces._codegen --out docs`
    entrypoint. Explicitly calls
    `stapel_core.django.openapi.swagger._register_jwt_auth_extension()`: the
    monolith's own `urls.py` triggers this drf-spectacular extension
    registration as a *global* side effect of importing `get_dev_urls()`
    (auth's harness gets it for free only because its co-mounted
    `stapel_gdpr.urls` happens to call the same registration); without it,
    protected endpoints would emit without their monolith
    `security: [{"JWTCookieAuth": []}]` entry — a real byte-identity delta,
    not a `$ref` component-closure gap.
- **`docs/schema.json`** (new) — drf-spectacular OpenAPI for workspaces only,
  canonical prefix; **`docs/flows.json`** (new) — empty array, this module has
  no `@flow_step` annotations yet.
- **Byte-identity** with the monolith aggregate's workspaces slice (paths under
  `/workspaces/api/` + their component closure) is **exact**: 8 paths, 13-
  component closure (`WorkspaceResponse`, `MemberResponse`, `StapelError`, …),
  zero diff vs the monolith's committed + freshly-regenerated aggregate.
  `errors.json` re-emission is also byte-identical to the previously-committed
  artifact. No sibling module needed co-mounting for closure (contract-
  pipeline.md §9 Q2): the workspaces slice's `$ref` closure is self-contained
  (workspaces + core's `StapelError`).
- **Gate:** `make contract` / `make contract-check`; `tests/test_contract.py`
  (drift + determinism + canonical-prefix + monolith-slice identity) is the
  CI-enforced gate. The monolith-slice identity test is skipped outside the
  workspace (module CI checks out only this repo).
- No friction from the workspaces brownfield User-model hardcode (already
  resolved upstream, see "fix: workspaces uses AUTH_USER_MODEL not concrete
  User") — the harness mounts cleanly on `AUTH_USER_MODEL="users.User"`,
  same as the existing test conftest.

## 0.4.0 — 2026-07-10

### Added — member listing: `?search=` + anchor pagination (BACKLOG G12)

`GET /{workspace_id}/members` now supports server-side filtering and cursor
pagination, so every downstream multi-tenant project with a people-picker stops
re-writing its own member listing (the G12 gap surfaced during a client import).

- **`?search=`** — case-insensitive substring match on the member's email **or**
  display name. Display name resolves the way the surface already presents a
  member (it joins `user`): full name → username → email, via a single
  `Coalesce(NullIf(Trim(Concat(first, last))), username, email)` expression.
- **Anchor pagination (stapel-core mandate).** The list is paginated with
  `stapel_core.django.api.pagination.AnchorPagination` — the cursor family that
  is **mandatory everywhere** in Stapel; `limit`/`offset` is banned because its
  windows slip rows (skip/dupe) under concurrent writes. The members endpoint
  now exposes the anchor surface (`anchor` / `limit` / `direction`) and returns
  the anchor envelope (`items`, `next_anchor`, `prev_anchor`, `has_next`,
  `has_prev`, `count`), exactly like the ETALON modules stapel-notifications /
  stapel-tasks (`CreatedAtAnchorPagination`).
- **Sort dropped to the anchor — display-name ordering removed.**
  `AnchorPagination` supports only a **single monotonic** anchor; it has no
  composite (`name,id`) cursor, so a display-name-sorted, insertion-safe window
  is not expressible. Members carry no `created_at`; the analog of the ETALON's
  `-created_at` is **`-invited_at`** (`auto_now_add` — the membership's creation
  timestamp), so the list is now ordered newest-invited-first. Consistency with
  the codebase-wide `limit`/`offset` ban wins over name ordering.
- **Breaking vs the un-tagged, un-published 0.4.0 dev surface only:** the earlier
  in-development shape (`limit`/`offset`, `{"members": [...]}`, stable
  display-name sort) is gone; the now-dead `MemberListResponse` DTO / serializer
  were removed. No released version ever exposed the `limit`/`offset` form.
- **OpenAPI contract:** `search` is declared as an `OpenApiParameter`; the
  `anchor`/`limit`/`direction` params + the `PaginatedMemberResponseList`
  response are emitted by the paginator, so they appear in `docs/schema.json`
  and the frontend codegen sees them — no shadow contract. The monolith
  aggregate's workspaces slice was regenerated in the same change, so the
  byte-identity gate (`test_matches_monolith_workspaces_slice`) stays green.

## 0.3.9 — 2026-07-06

### Changed — admin-suite AS-5: `@access` category rollout

Applies the `stapel_core.access` category decorators (admin-suite §0/AS-5 sweep,
docs/admin-suite.md) to this module's models and swaps the affected `ModelAdmin`
to `stapel_core.django.admin.base.StapelModelAdmin`.

- `WorkspaceInvitation` decorated `@access.secret` (`secret_fields = ("token",)`,
  pinned explicitly even though pattern detection on the field name would also
  catch it) — its bearer invite token is never returned by the invite-creation
  API and is now masked in the admin rather than shown in plaintext to any
  staff with model permissions; only a superuser can view/mutate the row.
  `@access.ops` was considered (the model has the `expires_at`/single-use shape
  the doc calls out) and rejected: `ops`'s admin-layer lockout is total — even a
  superuser cannot add/change/delete — and this repo has no application-level
  revoke endpoint, so `revoked_at` is only ever set via a direct admin edit;
  `ops` would have removed the only working revoke path, `secret` keeps it open
  to superusers.
- `Workspace` and `WorkspaceMember` stay undecorated (implicit
  `@access.standard`) — both are business tables staff work with directly
  (the admin-suite doc's own worked example names `Workspace`).
- Attribute-only change: no migrations
  (`makemigrations workspaces --check --dry-run` reports no changes).

## 0.3.8 — 2026-07-06

### Added — ru error catalog + bilingual error reference (i18n-shipping волна 2)

Reference-pattern application of the `stapel_core.i18n` catalog contour to the
`errors` domain (i18n-shipping.md §5), copied 1:1 from the stapel-auth pilot.

- `translations/errors.ru.json` — flat `{code: text}` ru catalog covering all
  52 keys, with `translations/.state.json` provenance sidecar. **50** keys
  seeded from the curated `stapel-translate` builtin fixtures (`origin:
  seed:stapel-builtin`, no tokens spent), **2** machine-translated (`origin:
  llm`, unreviewed). `translations/.errors.ru.llm-cache.json` is the
  committed, content-hash translation cache.
- `docs/errors.en.md` · `docs/errors.ru.md` — generated human-readable
  references; README + MODULE.md link both languages.
- `tests/test_error_i18n.py` — `check_translation_catalogs` gate + env-gated
  regen (`STAPEL_REGEN_ERROR_I18N=1`).


## 0.3.7 — 2026-07-06

### Added
- Declarative error registry with machine-readable remediation hints. Every
  `error.<status>.<name>` key the service raises now carries a `remediation`
  from the finite vocabulary (`retry | wait_and_retry | reauthenticate |
  verify | fix_input | contact_support | bug`), declared alongside the keys via
  `register_service_errors(..., remediation=...)`.
- `docs/errors.json` codegen artifact (`generate_error_keys`) — the
  language-agnostic registry of every key with its `status`, `{param}` slots,
  `remediation`, and English text — plus a byte-stable drift gate
  (`tests/test_error_keys.py`, mirrors the flow-doc/schema.json discipline).
- Canon remediation overrides where the frontend status+name heuristic lies
  (7 of 11 keys): `*_not_found` → `fix_input` (heuristic retries a 404,
  looping the lookup); `forbidden_workspace` → `contact_support` (not-a-member
  boundary — no field to fix, an owner must invite/promote); `last_owner…` →
  `fix_input` (self-serve precondition "transfer ownership first");
  `invitation_expired`/`invitation_revoked` → `contact_support` (dead,
  immutable token — only the owner can re-invite; the heuristic says retry for
  expired, which loops on a spent token). The reasoning is documented per key
  in `errors.py`.

### Changed
- Test settings now install `stapel_core.django.apps.CommonDjangoConfig` so the
  `generate_error_keys` management command is available to the drift gate.


## 0.3.6 — 2026-07-06

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.


## 0.3.5 — 2026-07-06

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_workspaces.tests`
  and `stapel_workspaces.tests.brownfield_users` subpackages are no longer
  listed in `[tool.setuptools] packages`). Added `[project.urls]`, completed
  the trove classifiers (MIT/OSI, Python 3.13, `Typing :: Typed`, OS
  Independent, `3 :: Only`, Development Status) and a `[tool.ruff]` lint
  section (single source shared with the git hooks/CI).


## 0.3.4 — 2026-07-05

### Changed
- OpenAPI: `@extend_schema` for `InternalPersonalWorkspaceView` (POST get-or-create
  personal workspace). Documents `request=None`, `200` →
  `InternalPersonalWorkspaceResponseSerializer` (`workspace_id`), `404` →
  `StapelErrorSerializer` — resolves the drf-spectacular "unable to guess
  serializer" error so the generated client is typed.

## 0.3.3 — 2026-07-05

### Fixed
- Reference `settings.AUTH_USER_MODEL` not the concrete `User` — unblocks
  custom user models / brownfield adoption. The `Workspace.owner`,
  `WorkspaceMember.user`, `WorkspaceMember.invited_by` and
  `WorkspaceInvitation.invited_by` FKs now target the swappable
  `settings.AUTH_USER_MODEL`, and `services`, `views` and
  `consume_auth_events` resolve the user via
  `django.contrib.auth.get_user_model()` instead of importing
  `stapel_core.django.users.models.User`. A host with a custom
  `accounts.User(AbstractStapelUser)` as `AUTH_USER_MODEL` no longer hits
  `ValueError: ... must be a "User" instance` when creating a
  `WorkspaceMember`. No migration/DB change: the FKs already deconstructed
  to `settings.AUTH_USER_MODEL` (the initial migration used
  `migrations.swappable_dependency`), so `makemigrations` reports no changes.


## 0.3.2 — 2026-07-05

### Fixed
- `user_id` in comm schemas typed uuid, was integer — rejected valid
  `user.deleted` events. `schemas/consumes/user.deleted.json` and
  `schemas/consumes/user.deletion_initiated.json` now type `user_id` as
  `{"type": "string", "format": "uuid"}`, matching the UUID-pk canonical
  user and the auth/gdpr producers.


## 0.3.1 — 2026-07-04

### Added
- `MODULE.md` — agent-facing extension-point map (part of the July 2026
  framework-wide documentation sweep). No functional changes.

## 0.3.0 — 2026-07-03

No functional changes — version alignment with the Stapel 0.3
release train; stapel-core dependency now `>=0.3.0,<0.4`.


## 0.2.1 — 2026-07-02

### Fixed
- `consume_auth_events` subscribes to the comm topic `user.registered`
  (the legacy Kafka topic it listened on is no longer published);
  personal-workspace bootstrap works again in microservices mode.
- `workspace.personal.created` published under its action name; legacy
  topic constant aliased.

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-07-02

### Added
- comm Function provider `workspaces.check_membership` (`functions.py`),
  registered from `AppConfig.ready()`. Same semantics as the internal HTTP
  membership endpoint: payload `{"workspace_id": str, "user_id": str}` →
  `{"is_member": bool, "role": str | null}`.
- Declared events are now actually emitted through `stapel_core.comm.emit`
  (transactional outbox): `workspace.created` on `create_workspace`,
  `workspace.member_joined` on `accept_invitation` and the personal-workspace
  bootstrap, and `workspace.personal.created` on the personal-workspace
  bootstrap.
- Invitation delivery: `create_invitation` requests a best-effort
  `workspace.invitation` notification via
  `stapel_core.notifications.request_notification` (variables:
  `workspace_name`, `inviter_name`, `accept_url`; `accept_url` uses the
  `FRONTEND_URL` setting when configured). Failures are logged and never
  break invitation creation.
- `stapel_core.signals.workspace_member_changed` is sent on member add
  (workspace create, invitation accept), role change ("updated") and
  removal ("removed").
- Cross-service membership cache invalidation
  (`stapel_core.django.workspaces.invalidate_membership_cache`) on member
  role change, member removal and invitation accept.
- Payload schema `schemas/emits/workspace.personal.created.json` and
  function schema `schemas/functions/workspaces.check_membership.json`.
- `py.typed` marker (PEP 561).
- Tests for the comm function, emitted event payloads (validated against
  the schema files), cache invalidation and the membership signal.

### Changed
- `schemas/emits/workspace.created.json` and
  `schemas/emits/workspace.member_joined.json`: `workspace_id`,
  `owner_id`, `user_id` are string UUIDs (matching the real payloads,
  which serialize UUIDs to `str`), `type`/`role` are enums.

## [0.1.0]

### Added
- Initial release: workspaces, members and RBAC, invitations, internal
  service API, GDPR provider.
