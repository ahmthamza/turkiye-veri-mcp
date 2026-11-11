# Gömülü ara sertifika

`globalsign_gcc_r46_ov_tls_ca_2025.pem` — "GlobalSign GCC R46 OV TLS CA 2025"
(veren: GlobalSign Root R46; geçerlilik 17 Eyl 2025 – 23 Haz 2029;
SHA-256 D1:60:E2:DE:4E:56:CB:10:B6:6C:1C:B0:AD:CB:79:CF:93:C7:8D:CD:9D:B3:0C:20:18:22:02:62:C0:40:63:F9).

Durum (2026-11-11 kontrolü): bddk.org.tr Ekim 2026'dan beri bu ara
sertifikanın imzaladığı yeni bir leaf kullanıyor (CN *.bddk.gov.tr, SAN'da
www.bddk.org.tr ve bddk.org.tr var; 6 May 2027'ye kadar geçerli) ve
zinciri eksiksiz gönderiyor. Yani düz `certifi` doğrulaması artık tek
başına çalışıyor; bu dosya yalnızca sunucu ara sertifikayı yeniden
göndermemeye başlarsa diye ek sigortadır.

Eski `globalsign_rsa_ov_ssl_ca_2018.pem` kaldırıldı: yeni leaf onunla
imzalanmıyor (sunucu onu hâlâ fazladan gönderiyor ama zincirde kullanılmıyor).

Kaynak: leaf sertifikanın AIA alanındaki resmi adres --
http://secure.globalsign.com/cacert/gsgccr46ovtlsca2025.crt (DER → PEM).
Sunucunun TLS el sıkışmasında gönderdiği kopyayla SHA-256 parmak izi birebir
aynı; `openssl verify -CAfile certifi -untrusted bu_dosya leaf.pem` → OK.

Kod (bddk.py `ssl_context`) certifi kök deposuna bu dosyayı ekleyip her
zaman TAM doğrulama yapar; doğrulamayı kapatan bir yedek yol yoktur.
Doğrulama başarısız olursa araç açık bir hata verir ve veri döndürmez.
