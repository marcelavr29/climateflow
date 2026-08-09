# ClimateFlow

Integrare HACS pentru **climatizarea automată a unei clădiri de evenimente** (biserică,
sală de evenimente): mai multe aere condiționate + centrala termică, conduse după un
program săptămânal de evenimente, cu **precondiționare adaptivă** — sistemul învață cât
de repede se încălzește/răcește sala și pornește singur cu exact atâta avans cât e nevoie
ca la începutul evenimentului temperatura să fie la țintă.

## Ce face

- **Program recurent săptămânal** cu regula „săptămânii de tranziție de lună”:
  un eveniment poate fi marcat să se țină *doar* sau *exceptând* săptămâna în care
  duminica pică pe luna următoare (ex.: joia sare, în locul ei se ține vinerea).
  Excepțiile nu se mai gestionează manual — sunt o regulă, codificată o singură dată.
- **Precondiționare adaptivă**: măsoară la fiecare rulare rata reală (°C/h) pe benzi
  de temperatură exterioară și își ajustează avansul de pornire (EMA, plafon configurabil).
- **Confort în timpul evenimentului**: menține ținta cu ventilatoarele plafonate la o
  treaptă maximă configurată (fără zgomot/curent). Plafonul e o constrângere fermă;
  dacă temperatura scapă, aprinde senzorul „Confort sub țintă” și notifică.
- **Iarna**: centrala termică e sursa principală; aerele intră automat ca **boost**
  pe încălzire când estimarea arată că ținta nu se prinde la timp.
- **Termostat pe senzorul de sală**: AC-urile primesc setpoint extrem (minimul/maximul
  unității) ca termostatul lor intern să nu se „sature" de la propriul aer — sala e
  ținută la țintă de ClimateFlow, pe senzorul real, cu histerezis (pornit/oprit).
- **Gardă anti-telecomandă**: în perioadele gestionate re-asertează setările modificate
  de la telecomenzi (cu debounce, limită de re-asertări și pauză anti-luptă).
- **Master switch** de activ/inactiv + buton/serviciu de oprire de urgență (totul OFF).
- La finalul evenimentului **stinge tot** și nu se mai atinge de nimic până la următorul.

## Instalare

1. Adaugă repo-ul în HACS (integrare custom) și instalează **ClimateFlow**.
2. Restart Home Assistant.
3. *Setări → Dispozitive și servicii → Adaugă integrarea → ClimateFlow*.
4. Alege: senzorul de temperatură interior, cel exterior, aerele condiționate
   (entități `climate`) și opțional centrala (entitate `climate`) + serviciul de notificări.

## Programul evenimentelor

**Programul vine preinstalat** — după setup sistemul funcționează imediat, fără nicio
configurare: marți 18–20, duminică 9:30–12 și 18–20, joi 18–19:30 (exceptând săptămâna
în care duminica pică pe luna următoare — atunci, în locul joii, vineri 18–20).

Panoul **ClimateFlow** din bara laterală arată programul (grilă săptămânală + agenda
următoarelor 7 zile cu regula de tranziție deja rezolvată) și permite ajustări rare:
mutat ore, sărit o ocurență, adăugat evenimente unice (nunți etc.).

- `week_filter`: `always` / `except_transition` (nu se ține în săptămâna în care duminica
  pică pe luna următoare) / `only_transition` (se ține doar atunci).
- Evenimente unice și din serviciul `climateflow.add_one_off`.
- Sări o singură ocurență: din agenda panoului sau butonul „Sări următorul eveniment”.

## Entități

| Entitate | Rol |
|---|---|
| `switch` Sistem activ | master — oprit ⇒ nu se comandă nimic |
| `switch` Gardă anti-modificare | re-asertarea setărilor schimbate de la telecomandă |
| `select` Sezon | `summer` / `winter` / `auto` (auto: după exteriorul de la momentul pornirii, cu histerezis) |
| `select` Ventilator maxim în eveniment | plafonul de viteză din timpul evenimentului |
| `number` Țintă temperatură, Avans maxim, Toleranță, Rate implicite, Supra-țintă | reglaje fine |
| `sensor` Următorul eveniment / Pornire precondiționare | ce urmează și când pornește |
| `sensor` Stare sistem | `idle` / `preconditioning` / `event` / `cooldown` |
| `sensor` Rată termică, Eșantioane, Punctualitate | diagnostic — ce a învățat sistemul |
| `binary_sensor` Precondiționare / Eveniment în curs / Confort sub țintă | stare live |
| `button` Sări următorul / Pornește acum / Oprește tot | acțiuni rapide |

## Cum calculează avansul

`avans = Δ°C necesar ÷ rata învățată (°C/h) × 1.15`, plafonat la „Avans maxim”.
Rata e o medie mobilă pe benzi de temperatură exterioară, per mod
(`cool` / `heat_boiler` / `heat_boiler_boost`), învățată din fiecare precondiționare
reușită. Până se adună eșantioane, se folosesc ratele implicite (numbers).

## Documentație

Ghidul complet (cum se calculează avansul, regula săptămânii de tranziție cu exemple,
boost-ul de iarnă, garda, întrebări frecvente): [docs/ghid-climatizare.md](docs/ghid-climatizare.md).

Pentru depanare: Setări → Dispozitive și servicii → ClimateFlow → ⋮ → *Descarcă
diagnosticele* (dump complet: configurare, stare live, rate învățate, istoric).

## Teste

```bash
pytest custom_components/climateflow/tests/
```
