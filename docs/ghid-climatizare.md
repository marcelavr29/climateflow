# Ghid ClimateFlow — climatizarea clădirii de evenimente

Acest ghid explică pe îndelete cum gândește ClimateFlow, ce reglaje ai la dispoziție și
cum să interpretezi ce vezi în panou.

## Problema pe care o rezolvă

O clădire de evenimente (biserică, sală) nu se încălzește instant: iarna, de la 15° la
22° poate dura ore. Dacă pornești manual centrala „cu ceva timp înainte", fie pornești
prea devreme (risipă), fie prea târziu (frig la începutul slujbei). Iar în timpul
evenimentului, aerele pe viteză mare fac zgomot și curent.

ClimateFlow automatizează tot lanțul:

1. **Știe programul** (preinstalat, cu tot cu regula joi/vineri).
2. **Învață clădirea** — cât de repede urcă/coboară temperatura, în funcție de cât e
   afară.
3. **Pornește singur** cu exact atâta avans cât e nevoie.
4. **În timpul evenimentului** menține temperatura cu ventilatoarele plafonate.
5. **Apără setările** de telecomenzi rătăcite.
6. **La final stinge tot.**

## Ciclul de viață al unui eveniment

```
        avans calculat            eveniment              final
IDLE ──────────────► PRECONDIȚIONARE ────► EVENIMENT ────► COOLDOWN ──► IDLE
      (așteaptă)      (viteză maximă,       (menține,       (totul
                       supra-țintă)          fan plafonat)    OFF)
```

- **Precondiționare**: pornește la `start eveniment − avans`. Rulează cu ventilatoare la
  maxim și cu o „supra-țintă" (implicit +0.5°) ca să existe o rezervă termică atunci
  când ventilatoarele se plafonează.
- **Eveniment**: la ora de start, temperatura comandată devine ținta exactă, iar
  ventilatoarele coboară la plafonul ales (implicit „Redus").
- **Cooldown**: la ora de final se închide ce a pornit ClimateFlow (cu 3 reîncercări
  dacă vreo unitate nu răspunde): aerele se sting, iar centrala coboară pe
  **temperatura de așteptare** (vezi mai jos). Apoi sistemul nu se mai atinge de nimic
  până la următorul eveniment.

## Cum se calculează avansul

```
grade de recuperat = țintă − temperatura interioară (iarna)
avans (min) = inerție + grade ÷ rata (°C/h) × 60 × 1.15, limitat la [10, Avans maxim]
```

**Inerția** (timpul mort) e intervalul de la pornire până când sala începe efectiv să
urce — la calorifere, 15–30 de minute în care temperatura nu se mișcă. Se învață
separat, per mod, și se adună la avans; rata se măsoară abia de la prima mișcare încolo.

**Rata** e partea deșteaptă: ClimateFlow măsoară la fiecare precondiționare câte grade
pe oră a urcat sala în realitate și reține valoarea pe **benzi de temperatură
exterioară** (sub −5°, −5–0°, 0–5°, 5–10°, peste 10°), separat pentru fiecare mod
(răcire / doar centrala / centrală + aere). Media e „alunecoasă" (EMA): valorile noi
contează 30%, istoricul 70% — se adaptează, dar nu sare la fiecare măsurătoare.

Până se strâng eșantioane, se folosesc **ratele implicite** din Setări. Primele
săptămâni pot fi imprecise — e normal; urmărește tab-ul Învățare cum se umplu benzile.

Rata se măsoară **până în momentul în care sala atinge ținta**, nu pe toată fereastra:
timpul în care sala doar stă la țintă nu diluează măsurătoarea (altfel sistemul ar
învăța doar „mai lent", niciodată „mai rapid").

Dacă ținta **nu** a fost atinsă și sala abia s-a mișcat, rularea nu se aruncă: sistemul
învață „foarte lent" (doar dacă senzorul a raportat în acel interval), ca data viitoare
să pornească mai devreme.

Eșantioanele suspecte se aruncă automat: rulări sub 15 minute, delte sub 0.3°, rate
peste 10°C/h (senzor defect), rulări în care s-a umblat la gardă sau la țintă, ori
senzorii au picat.

## Regula săptămânii de tranziție

Programul spune: joia se ține slujba, **cu excepția** săptămânii în care duminica pică
pe luna următoare — atunci nu se ține joia, ci **vinerea**.

Formal: o zi e „în săptămâna de tranziție" dacă prima duminică de la acea zi încolo
(inclusiv ziua însăși, dacă e duminică) pică în altă lună calendaristică.

Exemple:
- joi **29 oct 2026** → duminica ei e 1 nov → tranziție → joia sare, vineri 30 oct se ține;
- joi **5 nov 2026** → duminica ei e 8 nov, aceeași lună → joi normal;
- joi **31 dec 2026** → duminica ei e 3 ian 2027 → tranziție (regula trece peste ani).

Nu ai nimic de făcut: evenimentul de joi are filtrul „exceptând tranziția", cel de
vineri „doar în tranziție", iar agenda din panou arată mereu săptămâna deja rezolvată.
Când e săptămână de tranziție, pe Acasă apare și o notă.

## Iarna: centrala + aerele (boost)

Centrala e sursa principală de căldură. Aerele intră pe încălzire („boost") în trei
situații:

1. **Din start** — dacă și cu avansul maxim centrala singură n-ar prinde ținta
   (matematic: rata necesară > rata centralei).
2. **Din mers, în precondiționare** — dacă estimarea arată că ținta nu se atinge la
   timp (o singură escaladare, nu oscilează; nu în primele 15 minute, cât caloriferele
   abia se încălzesc). După o jumătate de oră de la prima mișcare, estimarea se face
   după **viteza reală** cu care urcă sala, nu după cea presupusă. În acel moment sistemul **învață rata reală a centralei
   singure**, deci data viitoare pornește mai devreme și are mai rar nevoie de boost.
   Porțiunea de boost se învață separat, pe modul „centrală + aere".
3. **În timpul evenimentului** — dacă temperatura stă sub țintă peste 10 minute; dar
   aici aerele intră **tot cu ventilatorul plafonat** (confortul rămâne prioritar).

Fiecare boost apare în istoricul din tab-ul Învățare (⚡), ca să vezi cât de des e
nevoie de el. Dacă boost-ul pornește mereu **din start** (situația 1), învățarea nu
mai are ce face: centrala nu ajunge nici cu avansul maxim. După trei asemenea porniri
în 30 de zile primești o notificare și o notă în tab-ul Învățare cu avansul de care ar
avea nevoie centrala singură — crește „Avansul maxim" până acolo.

## Temperatura de așteptare a centralei

La finalul evenimentului centrala **nu se stinge**: trece pe o temperatură de așteptare
(implicit 18°, reglabilă 5–22° din Setări). O clădire lăsată să se răcească de tot cere
ore de avans și boost la următorul eveniment; ținută la 18–19°, recuperarea e scurtă și
previzibilă.

- Se aplică doar dacă centrala a lucrat în acea rulare (iarna). Vara nu e atinsă deloc.
- Se aplică și la „Oprește tot" și la oprirea „Sistem activ": aerele se sting, centrala
  coboară pe așteptare.
- E trimisă o singură dată, la final, și nu e apărată de gardă — o poți schimba manual
  de pe termostat între evenimente.
- Comutatorul „Centrala rămâne pe temperatura de așteptare" oprit ⇒ comportamentul
  vechi: centrala se stinge la final.

## Termostatul pe senzorul de sală (de ce AC-urile nu primesc ținta)

Termostatul intern al unui aparat de aer condiționat măsoară aerul din **jurul
unității** (de obicei sus, la aspirație). La setpoint 22°, aerul rece pe care îl suflă
chiar el ajunge repede înapoi la senzorul lui → aparatul „crede" că încăperea s-a răcit
și taie compresorul, deși sala e încă caldă. Efectul clasic: zona de lângă aparat e
rece, restul sălii nu.

De aceea, cu comutatorul **„Termostat pe senzorul de sală"** activ (implicit),
ClimateFlow comandă aparatelor **setpoint-ul extrem** — minimul unității la răcire,
maximul la încălzire — ca să nu se auto-oprească niciodată. Sala e ținută la țintă de
ClimateFlow însuși, pe senzorul real de sală, cu histerezis pe toleranță:

- sala peste țintă + jumătate de toleranță (la răcire) → aparatele pornesc (setpoint extrem);
- sala sub țintă − toleranță → aparatele se opresc de tot;
- senzorul de sală indisponibil peste 3 minute → aparatele trec pe ținta normală
  (termostatul lor intern) până revine senzorul, plus notificare;
- garda apără inclusiv starea „oprit" și setpoint-ul extrem (dacă cineva îl mută pe
  22° de la telecomandă, revine);
- senzorul de sală care n-a mai raportat nimic de 2 ore (baterie moartă, rămas pe
  ultima valoare) e tratat tot ca indisponibil;
- dacă aparatul își limitează singur setpoint-ul (comandat 12°, raportează 16°),
  ClimateFlow adoptă valoarea lui în loc să o corecteze la nesfârșit.

Centrala termică **nu** e afectată de acest mod — ea are termostat de cameră adevărat
și primește în continuare ținta normală.

## Garda anti-telecomandă

În perioadele gestionate (precondiționare + eveniment), orice abatere de la setările
comandate (mod, temperatură ±0.5°, viteză ventilator) e corectată automat după ~20 de
secunde (timp în care telecomanda apucă să-și termine rafala de comenzi).

Ca să nu intre în duel cu o persoană insistentă: maximum 3 corecții per aparat în 5
minute, apoi pauză de 5 minute + notificare „cineva modifică repetat". Stările
`unavailable` nu sunt tratate ca abateri (poate fi doar integrarea picată).

Garda se poate opri complet din Setări (sau switch-ul „Gardă anti-modificare").

## Sezonul

- **Vară** — aerele răcesc; centrala nu e atinsă.
- **Iarnă** — centrala încălzește; aerele intră doar ca boost.
- **Auto** — decis la fiecare pornire de precondiționare, după temperatura exterioară:
  sub 15° → iarnă, peste 22° → vară. Între ele decide **nevoia sălii**: sala sub
  țintă → încălzire, peste țintă → răcire; dacă sala e deja în toleranță, rămâne pe ce
  a fost. Sezonul rezolvat se păstrează și peste un restart HA.

## Întrebări frecvente

**Dacă pornesc aerul manual între evenimente?**
Nimic — între evenimente ClimateFlow nu se atinge de aparate. Doar în precondiționare
și eveniment comandă și apără setările.

**Dacă restartez Home Assistant în timpul unui eveniment?**
Se recuperează singur: starea se deduce din ceas + program, nu se pierde nimic în afara
eșantionului de învățare din rularea respectivă. Dacă HA a fost oprit chiar peste
finalul evenimentului, la pornire stinge aparatele rămase pornite.

**Ce face „Oprește tot"?**
Stinge aerele (centrala trece pe temperatura de așteptare) și lasă evenimentul curent (sau cel pentru care se precondiționa) oprit —
nu mai repornește singur. În agendă apare ca „sărit"; îl reactivezi de acolo sau cu
„Pornește acum". Următorul eveniment pornește normal.

**Ce face „Pornește acum"?**
Dacă următorul eveniment începe în cel mult „Avansul maxim", pornește precondiționarea
spre el. Altfel pornește o rulare manuală de 60 de minute (nu încălzește zile întregi
spre un eveniment îndepărtat).

**Am schimbat ținta / ventilatorul în timpul evenimentului — se aplică?**
Da, la următorul minut comenzile se retrimit cu valorile noi.

**Cum sar peste un eveniment (ex. nu se ține marți)?**
Din agenda de pe Acasă → „⏭ sari" pe ocurența respectivă (sau butonul „Sări următorul
eveniment"). Doar acea apariție e sărită; săptămâna următoare revine normal.

**Dacă modific programul din panou în timpul unui eveniment?**
Nu se întrerupe nimic: salvarea programului nu mai reîncarcă integrarea. Orele noi sunt
preluate la următorul minut; comenzile se retrimit doar dacă ai schimbat ținta
evenimentului în curs.

**Cum adaug o nuntă?**
Tab-ul Program → „Evenimente unice" → completezi data/ora/ținta → Salvează. Sau
serviciul `climateflow.add_one_off` dintr-o automatizare.

**De ce n-a ajuns la țintă la începutul evenimentului?**
Vezi Învățare → istoric (✗ întârziat) și benzile: dacă banda condițiilor de afară n-are
încă eșantioane, s-a folosit rata implicită — care poate fi optimistă. Crește „Avansul
maxim" sau ajustează „Rata implicită încălzire" în jos; după câteva evenimente sistemul
se calibrează singur.
