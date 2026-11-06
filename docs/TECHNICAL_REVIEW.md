# Pliqra — teknik mimari değerlendirme

- **Tarih:** 6 Kasım 2026
- **İncelenen başlangıç:** 2f5abc6 — Pliqra 1.1
- **Düzeltme sürümü:** 1.2

[← Pliqra](../README.md) · [Mimari](ARCHITECTURE.md) · [Doğrulama kaydı](RELEASE.md)

## Yönetici değerlendirmesi

Pliqra'nın hedefiyle en uyumlu kurgu, modüler bir uygulamanın tek Docker imajında sunulmasıdır. Model sunucusu, kimlik sağlayıcı ve guardrail motoru dış kaynaklar olarak kalmalı; Pliqra bu kaynakların erişim, sözleşme, yönlendirme ve kullanım denetimini üstlenmelidir. Bu aşamada mikroservislere bölmek, dağıtım kolaylığını azaltırken çözülen bir üretim gereksinimi göstermiyor.

Ürünün ayrışma noktası yalnızca yönlendirme veya kota değildir: uygulamanın kabul ettiği sözleşme, çalıştırılan politika ve gerçekleşen çağrının kanıtları aynı akışta izlenebiliyor. İlan edilen yetenek, doğrulanmış davranış, raporlanan kullanım ve tahmin edilen maliyetin ayrı tutulması doğru bir temel.

Başlangıç sürümünde bu niyetle uygulama arasında boşluklar vardı. Özellikle serbest API alanları, yalnızca mesajı kontrol eden yanıt işleme, kabul anından önce yapılan yetki kontrolleri ve birbirinden bağımsız sayaç güncellemeleri güven sınırını zayıflatıyordu. Aşağıdaki bulgular kod ve regresyon testleriyle giderildi.

**Sonuç:** 1.2, belgelenen tek çalışma alanı ve tek sunucu süreci hedefi için daha tutarlı bir temel sağlar. Bu değerlendirme; bağımsız güvenlik sertifikası, bütün sağlayıcılarla uyumluluk veya HA yeterliliği anlamına gelmez.

## Kapsam ve yöntem

- Yönetim, kimlik/yetki, standart/named/protocol inference yolları, guardrail zinciri, kota/bütçe kayıtları, cache, sağlık kontrolleri, şifreleme ve restore akışları incelendi.
- Riskli davranışlar izole veritabanları ve kontrollü upstream yanıtlarıyla tekrarlandı; değişiklikler mevcut testlerle birlikte doğrulandı.
- Yarış durumları iki çağrıyı aynı hazırlık noktasında buluşturan testlerle; kayıt tutarlılığı hata enjeksiyonuyla; schema yürütmesi süre aşımı ve event-loop canlılığıyla sınandı.
- Gerçek şirket verisi, kullanıcı parolası veya canlı model/NeMo servisi kullanılmadı. Bu çalışma gerçek IdP/NeMo kabul testinin ve kapsamlı yük/sızma testinin yerine geçmez.

P1, bu dağıtım hedefinde önce giderilmesi gereken politika, veri veya erişilebilirlik sorununu; P2 ise işletim ve kayıt doğruluğu sorununu ifade eder. Bunlar CVSS puanları değildir.

## Bulgular ve uygulanan düzeltmeler

| ID | Öncelik | Başlangıçtaki sorun | 1.2'de uygulanan düzeltme | Kanıt |
|---|---|---|---|---|
| TA-01 | P1 | Çıktı maskelemesi mesajı değiştirirken logprobs, provider debug, model/id veya usage içindeki kontrol edilmemiş metin dönebiliyordu. Yerel kurallar protokolün role/type değerlerini de içerik sanabiliyordu. | Çıktı kontrolü olan çağrılar için kontrollü yanıt zarfı; yerel metadata ve doğrulanan mesaj alanları. Metin alanlarında nesne/listeler reddediliyor. Metin dönüşümü protokol rollerini ve tür etiketlerini değiştirmiyor. | JSON/SSE yardımcı kanal ve role/keyword regresyonları |
| TA-02 | P1 | Serbest ek istek alanları, belirsiz token kontrolleri ve eski functions/function_call biçimi politika anlamını aşabiliyordu. | Ayrı, tipli ve kapalı inference şeması; tek output-limit alanı; modern/eski tool biçimleri için ortak yetenek ve izin denetimi. Desteklenmeyen alanlar upstream çağrısından önce 422 alır. | Bilinmeyen kontrol, çift limit, hatalı stream options ve legacy tool testleri |
| TA-03 | P1 | HTTP doğrulaması/hazırlık ile kota kabulü arasındaki kullanıcı, oturum, servis veya doğrulanmış yetenek değişiklikleri eski bilgiyle çağrı başlatabiliyordu. | Tutarlı hazırlık görüntüsü ve atomik kabul işlemi. Güncel kullanıcı/anahtar/oturum, proje/servis yetkisi, servis fingerprint'i, gateway politikası ve uygun deployment'lar kabul anında yeniden denetleniyor. | Kullanıcı/oturum iptali, servis değişimi ve doğrulama kaybı testleri |
| TA-04 | P1 | JSON Schema regex/ref işlemleri ana async döngüsünde çalışıyordu; pahalı bir doğrulama diğer çağrıları durdurabiliyordu. | Guardrail ve değerlendirmeler ortak, en fazla iki geçici schema işçisi kullanıyor. Kuyruk dahil süre sınırı; iptalde öldürme/reap; Linux'ta adres alanı ve CPU sınırı; dış referans çözümleme kapalı. | Zor regex sırasında heartbeat, timeout ve çocuk süreç kapanışı testi; pozitif schema testleri |
| TA-05 | P1 | SSE/NDJSON satırı büyüklüğü sınırsızdı. Discovery cevabı tamamen belleğe alındıktan sonra ölçülüyordu. Eksik completion biçimleri başarılı/doğrulanmış sayılabiliyordu. | Okuma sırasında boyut sınırları, strict JSON ve minimum completion doğrulaması. Bozuk stream kontrollü incomplete/error üretir; küçük frame'ler biriktirilmeden teslim edilir. | Büyük stream frame'i, ilk frame teslimi, hatalı completion ve mevcut Unicode/eksik stream testleri |
| TA-06 | P1 | Bir fallback'in 30.000 bilinen token'ına rağmen, sonraki denemenin kullanımı belirsizse gateway sayacı 4.350 rezervasyonda kalabiliyordu. Event ve ledger sonuçları ayrı yazılıyordu. | Kısmi kullanım en az bilinen toplamı ve mevcut rezervasyonu korur. Event ve gateway settlement aynı transaction'da commit edilir. | Kısmi fallback ve settlement hata enjeksiyonu testleri |
| TA-07 | P1 | Yeni guardrail secret alanı açılış anahtar kontrolüne katılmıyordu. Geçerli biçimli ama veriyle uyuşmayan bir anahtarlı arşiv restore edilebiliyordu. | Tek şifreli kaynak kataloğu; guardrail dahil akış halinde doğrulama. Restore, key/ciphertext ve foreign-key tutarlılığını mevcut veriyi değiştirmeden denetler. | Guardrail-only missing/wrong key ve bozuk arşivin mutasyon öncesi reddi |
| TA-08 | P2 | Daha yeni veritabanı açıldığında mevcut migration kodu sürüm numarasını geriye yazabiliyordu. | Desteklenenden yeni schema, migration başlamadan reddedilir. | Schema 999'un reddi ve sürümün değişmeden kalması |
| TA-09 | P2 | Paralel round-robin çağrıları aynı eski sayaçtan hazırlanıp aynı modele gidebiliyordu. | Seçim sırası ve kalıcı cursor ilerletme, kabul transaction'ına taşındı; reddedilen çağrı cursor tüketmiyor. | Aynı anda hazırlanan iki çağrının iki deployment'a dağıtılması |
| TA-10 | P2 | Fallback'ten cache'e yazılan yanıt dönerken header/event ilk adayı gerçek model gibi gösterebiliyordu. | Şifreli cache kaydı üretici deployment kimliğini taşır. Cache hit'in event/header bilgisi bu kaynağa dayanır; upstream token/maliyet sıfır kalır. Eski cache biçimi miss kabul edilir. | Fallback sonrası cache provenance ve sıfır kullanım testi |
| TA-11 | P2 | Bekleyen sağlık kontrolü, değişen endpoint veya restore edilmiş alan üzerine eski sonucu yazabiliyordu. Açık provider testi restore bariyerine dahil değildi. | Sağlık kontrolü başarı/hata yazıları generation/config kontrolünü transaction içinde yapar. Provider testleri maintenance/concurrency ve restore bariyerine katılır. | Başarı/hata × endpoint/workspace değişimi ve açık check sırasında restore reddi |
| TA-12 | P2 | Named /g/ yolu standart API'nin no-store sınırına dahil değildi. | Aynı cache-control sınırı bütün inference adreslerine uygulandı. | Named model listesi ve completion için no-store testi |

Kaynaklar: [inference sınırı](../pliqra/inference_schema.py), [kabul ve yürütme](../pliqra/gateway.py), [politikalar](../pliqra/policies.py), [schema yürütücüsü](../pliqra/schema_validation.py), [worker](../pliqra/schema_worker.py), [şifreleme](../pliqra/security.py), [kurtarma/sağlık](../pliqra/operations.py), [regresyon testleri](../tests/test_architecture.py).

## Düzeltilmiş yürütme modeli

~~~mermaid
flowchart LR
    Request[Tipli istek] --> Prepare[Tutarlı servis/politika görüntüsü]
    Prepare --> Admit[Atomik kabul]
    Admit --> Auth[Güncel kimlik + oturum + erişim]
    Auth --> Limits[Kota + bütçe + concurrency + cursor]
    Limits --> Input[Giriş kontrolleri]
    Input --> Model[Uygun model / güvenli fallback]
    Model --> Output[Yanıt biçimi + gerekiyorsa kontrollü zarf]
    Output --> Checks[Çıktı kontrolleri]
    Checks --> Commit[Atomik event + ledger sonucu]
    Commit --> Deliver[JSON / kontrol edilmiş SSE]
    Schema[Geçici, sınırlı schema işçileri] -.-> Input
    Schema -.-> Checks
~~~

Bu şekil tam yanıt/çıktı kontrollü akışı gösterir. Canlı upstream streaming'de giriş kontrollerinden sonra frame'ler aktarılır; terminal veya hata durumunda kayıtlar kapatılır. Ağ teslimiyle veritabanı arasında dağıtık bir “exactly once” garantisi verilmez.

## Korunması gereken mimari kararlar

1. **Modüler monolith ve tek kalıcı volume.** Bu, taşınabilir kurulum hedefiyle uyumlu. Schema işçileri API veya veritabanı sahibi değildir; yalnızca geçici doğrulama yapar.
2. **Kontrol düzlemi ve yürütme sınırı.** Yönetim API'leri, tipli inference sözleşmesi ve provider I/O birbirinden ayrılmalı. Yeni vendor alanı eklemek, yalnızca JSON'u aktarmak yerine kota/yetenek etkisini tanımlamayı gerektirir.
3. **İlan, kanıt ve fiyat ayrımı.** Başarılı format kontrolü genel model kalitesi veya semantik güvenlik kanıtı değildir. Kullanım bilinmiyorsa bilinmiyor olarak kalmalı; maliyet tahmini fatura gibi sunulmamalı.
4. **Açık hata davranışı.** Yetki iptali 401/403; değişen servis/politika kabulü 409; geçici uygunluk sorunu 503; desteklenmeyen istek 422. Başlamış upstream stream yeniden oynatılmaz.
5. **İzlenebilir ve sınırlı yan etkiler.** Test/probe/evaluation açıkça başlatılır. Ham içerik varsayılan olarak saklanmaz. Çocuk süreçler ve geçici servisler iş sonunda kalmamalı.

1.2 ile inference uygulama revizyonu fingerprint hesabına eklendi. Eski binding ve cache kayıtları yeni yürütme anlamını sessizce devralmaz; uygulamalar yeni sözleşmeye açıkça bağlanır. Wire protokolü draft 0.1 olarak kalır.

## Doğrulama ve kabul

- Test paketi: **92 test**; ilk 51 test korunarak 41 mimari regresyon eklendi.
- Host Python 3.14 ve Linux/Python 3.13 Docker doğrulaması.
- Boş Docker alanında gerçek HTTP kontrolü: 1.2.0 health, ilk yönetici kurulumu, anonim erişimin reddi, schema işçisinin kabul/ret sonuçları, no-store ve bilinmeyen istek alanının 422 ile reddi.
- Kapsam: olumlu inference, Unicode streaming, fallback, kimlik/SCIM/SAML, quota/budget, backup/restore, gateway/guardrail ve yukarıdaki yarış/hata durumları.
- Gerçek vendor kabulü, soak testi, bağımsız sızma testi ve dağıtık ortam testleri bu çalışmada yapılmadı.

## Dağıtım kararı ve sonraki eşikler

**Mevcut hedef için:** tek çalışma alanı, tek sunucu süreci ve kalıcı volume ile kontrollü dağıtım; kurumun gerçek endpoint ve kimlik politikalarıyla kabul testi şartıyla.

**Yeni bir mimari gerektiren eşikler:**

| Gereksinim | Gerekli çalışma |
|---|---|
| Birden fazla aktif replika / HA | Paylaşılan transaction modeli, distributed quota/concurrency, PostgreSQL ve failover kabul testleri |
| Fiziksel tenant izolasyonu | Ayrı veri/anahtar/kimlik sınırları; mevcut project yetkilendirmesi bunun yerine geçmez |
| Çok büyük raporlama hacmi | Gerçek iş yüküyle kapasite ölçümü; gerekirse ayrı analitik okuma modeli |
| Gerçek NeMo/IdP uyumluluk matrisi | Kurumun sürüm, claim, sertifika ve policy yapılandırmalarıyla uçtan uca testler |
| Performans/SLO taahhüdü | Yeni worker ve guardrail maliyetini içeren yük/soak ölçümü; eski sentetik benchmark tekrar kullanılmamalı |

Bu eşikler mevcut sürümün yaptığı işler gibi pazarlanmamalı. Kapsam ve uyumluluk sınırları [işletim rehberinde](OPERATIONS.md), [gateway rehberinde](GATEWAYS.md) ve [doğrulama kaydında](RELEASE.md) korunuyor.

## 2.0 architecture follow-through — 6 November 2026

The previous future-scope list is now linked to delivered features and acceptance evidence in [ROADMAP.md](ROADMAP.md). Storage is explicit rather than an opaque SQL compatibility shim: portable schema/data operations are shared; backend-specific JSON/date/introspection paths are explicit. A real PostgreSQL test caught and corrected a remaining SQLite JSON operation in the onboarding dashboard. Admission and configuration writes use one schema lock, while upstream calls remain outside transactions. This trades write parallelism for deterministic policy correctness.

Replica leases fence stale processes; recovery retains uncertain charges and never replays started inference. Management writes participate in restore barriers. Logical restore is transactional, bounded, key-validated and isolated to the selected schema. Organization sessions and keys are separate, with root-directory administration distinct from tenant data permissions.

Native/binary routes share authentication/admission/accounting and apply guardrails only to inspectable content; incompatible required stages fail closed. Provider-native streaming is labeled buffered. External score evidence includes dataset/judge/service versions, and publication rejects stale or failed evidence. The protocol remains draft and its executable core profile makes no vendor-safety or ecosystem claim.

[2.0 validation](RELEASE.md) and [deployment limits](PLATFORM.md) record real local interoperability separately from synthetic provider fixtures and unavailable customer tenants. Database HA infrastructure and production capacity still require operator validation.
