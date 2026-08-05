# Gömülü ara sertifika

`globalsign_rsa_ov_ssl_ca_2018.pem` — "GlobalSign RSA OV SSL CA 2018".

Neden burada: bddk.org.tr sunucusu TLS el sıkışmasında yalnızca kendi
(leaf) sertifikasını gönderiyor, onu imzalayan bu ara sertifikayı
göndermiyor. Tarayıcılar eksik halkayı sertifikadaki AIA adresinden
otomatik indirip zinciri tamamlıyor; Python'un `ssl` modülü bunu yapmaz
ve `CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate`
hatası verir.

Kaynak: sertifikanın kendi AIA alanında yazan resmi adres --
http://secure.globalsign.com/cacert/gsrsaovsslca2018.crt

Doğrulama (2026-08-05): bu dosya `-untrusted` olarak verildiğinde,
bddk.org.tr'nin canlı leaf sertifikası certifi kök deposuna karşı
`openssl verify` ile OK döndü. Yani bu, güvenilir GlobalSign Root CA -
R3'e kadar giden gerçek zinciri tamamlıyor; doğrulamayı devre dışı
bırakmıyoruz, eksik halkayı tamamlıyoruz.

Geçerlilik: 21 Kasım 2028'e kadar. BDDK zincirini düzeltirse ya da
sertifika değişirse bu dosya güncellenmeli; kod zaten önce normal
doğrulamayı denediği için, bu dosya gereksizleştiğinde de sorun çıkmaz.
