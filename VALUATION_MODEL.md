# Convertible-bond valuation model

## Production scope

Version `tf_split_tree:v3` is the production default. It is a one-factor,
constant-parameter Tsiveriotis-Fernandes (TF) lattice. `simple_crr` remains
available as a diagnostic comparison, but it discounts conversion value at the
risky cash rate and should not be used as the primary fair value.

This choice is deliberately pragmatic. The current data model has observable
stock, volatility, rates, borrow, dividends, and a credit spread, but it does
not yet carry a calibrated default intensity, recovery, or equity jump on
default. Adding those as hidden constants would give a more sophisticated name
to less auditable assumptions. The TF split uses the inputs the terminal can
actually explain and replay.

## Mathematics

The stock follows the risk-neutral effective-carry process

```text
dS / S = (r - q - b) dt + sigma dW,
```

where `r` is the risk-free rate, `q` dividend yield, and `b` stock-borrow cost.
The usual Cox-Ross-Rubinstein (CRR) step is

```text
u = exp(sigma sqrt(dt)),  d = 1/u,
p = [exp((r-q-b)dt) - d] / (u-d).
```

An invalid `p` is never clipped. At exactly zero volatility the lattice
collapses to the deterministic forward path. If a requested coarse step makes
CRR invalid, the engine uses a recombining equal-probability tree with

```text
a = sigma sqrt(dt)
m = (r-q-b)dt - log(cosh(a))
u = exp(m+a),  d = exp(m-a),  p = 1/2.
```

This preserves `E[S(t+dt)/S(t)] = exp((r-q-b)dt)` instead of silently changing
the stock martingale.

At each node the TF value `V` is split into an equity/conversion component `E`
and a cash-only component `C`:

```text
E = exp(-r dt)       [p E(up) + (1-p) E(down)]
C = exp(-(r+s) dt)  [p C(up) + (1-p) C(down)]
V = E + C,
```

where `s` is the credit spread. Conversion sets `(E,C)=(kS,0)`; a cash put
sets `(E,C)=(0,P)`. Coupon cash is added at its payment node before the holder
chooses whether to continue or convert. The terminal payoff is
`max(redemption + final coupon, kS)`, not final coupon plus conversion.

Holder and issuer rights are applied simultaneously:

```text
L = max(eligible conversion value, eligible put price)
H = max(call price, forced-conversion value)
node value = max(L, min(continuation, H)).
```

All active call schedules are retained. If `L > H`, the engine keeps the
holder floor and reports a contract-priority warning instead of allowing an
issuer call to erase a holder put.

Coupon dates are inferred backwards from maturity because normalized contracts
currently contain frequency rather than explicit payment dates. Multiple cash
flows mapped to a coarse node are summed, not collapsed. The reported bond
floor is a risky, **uncallable** cash-only backward valuation with coupons and
scheduled holder puts. Callable structures emit a warning because an issuer
call can cap fair value below that investment-value reference. Fair value and
the cash reference are reported on the market quote's clean/dirty basis, using
the same accrued-interest schedule as traded yields. Coupon-date puts retain
that day's coupon; other scheduled puts include accrued interest by assumption.

A conversion start stated as the day after three months from closing is dated
from the recorded closing date. Full dividend protection is approximated by
zero dividend carry, with the effective rate reported alongside the input rate.
Early-redemption call amounts tied to a stated yield are updated at each node.
Soft-call observation windows and notice periods remain approximations.

For a maturity-only conversion, the final TF step is integrated analytically.
With cash redemption `F`, ratio `k`, strike `K=F/k`, and `c=r-q-b`:

```text
d1 = [ln(S/K) + (c + sigma^2/2)dt] / [sigma sqrt(dt)]
d2 = d1 - sigma sqrt(dt)
E = k S exp(-(q+b)dt) N(d1)
C = F exp(-(r+s)dt) N(-d2).
```

This removes the artificial digital jump caused by assigning an entire terminal
lattice atom to either cash or equity.

## Implied volatility

Implied volatility is conditional on the selected credit spread, curve,
dividend, borrow, and contract interpretation. A single CB price cannot jointly
identify volatility and credit. The price/volatility curve is sampled for every
structure, including noncallable bonds; sampled interior turning points are
refined before solving. Flat, rootless, multiple-root, or unconverged cases are
reported as not identifiable rather than returning an arbitrary percentage.
When a lattice price jumps across the target and the IV bracket can no longer
shrink, the batch pricer tries at most two finer grids (up to 1,000 steps). It
reports the first refined IV only when the two finer-grid answers differ by no
more than 0.1 percentage point; otherwise IV remains unavailable. The fair
value and other outputs stay on the requested grid, and IV diagnostics record
the refinement and validation grids. Refinement is not applied to ambiguous,
flat, or out-of-range prices.

## Yield to maturity and yield to put

Prospectus-stated yields and calculated yields are separate values. At issuance,
the terminal independently solves from the gross issue price (excluding
brokerage) and the closing/issue date, then compares the result with the quoted
prospectus yield in basis points. A material mismatch blocks approval rather
than overwriting the source quote.

Once an observed CB price exists, every dated market row calculates a promised
cash-flow YTM. It also calculates yield to every future deterministic scheduled
holder put and exposes the earliest unexpired put as the row's primary
yield-to-put. Event puts are excluded. For nominal annual yield `y`, compounding
frequency `m`, year fraction `t_i`, dirty price `P`, and promised cash flows
`CF_i`, the solver finds the unique root of

```text
P = sum_i CF_i / (1 + y/m)^(m t_i).
```

The root is bracketed so negative yields are supported. Market price, not fair
value, parity, or bond floor, is the input. Calls and conversion optionality do
not alter these conventional cash-flow yields, so they must not be read as
yield-to-worst.

Where the contract explicitly identifies clean/dirty quote and day-count
conventions, they are used. Otherwise the calculation labels its assumptions:
clean price, maturity-anchored coupon dates, and ACT/365.25. Accrued interest is
added to an assumed clean coupon-bond quote. A non-coupon-date put is assumed to
pay accrued coupon interest. These fallbacks are exact enough to reconcile the
current zero-coupon contracts, but coupon-bearing results require the reviewer
to confirm coupon dates/stubs, day count, quote convention, settlement lag, and
whether a put payoff includes accrued interest.

For supported FX-linked cash flows, traded yields use the economic-currency
cash amounts and translate the observed quote at current spot FX. They are
conditional spot-FX yields, since the actual settlement fixing can differ.

Market rows currently use the quote as-of date as settlement because the
normalized contract does not yet carry a secondary-market settlement lag and
calendar. The API, CSV, and dashboard warnings label this same-day settlement
assumption; no T+1/T+2 convention is invented.

## Currency roles

For every bond, `bond.currency` records the legal denomination,
`bond.economic_currency` the linked principal risk, and
`bond.settlement_currency` the payment currency. The risk-free curve follows
the economic currency. An automatic curve in another currency is rejected;
if no matching source is configured, the user must enter a manual rate.

`conversion.fixed_exchange_rate_basis_currency` identifies the currency in
the fixed conversion quote. It may be the legal or economic currency, and
`conversion.fixed_exchange_rate_units` must pair that basis with the stock
currency. When economic and stock currency coincide, a settlement-linked bond
is valued in that economic currency, including redemption and exercise cash;
its present value is then translated to the quote currency at spot. Structures
without the FX pair required for that translation are rejected rather than
priced with a fixed legal-currency redemption. The payment-date FX fixing and
its volatility remain outside the one-factor model.

A current government curve applied to older market rows is labeled a current
curve scenario in the API and dashboard. Its yield is used as a flat continuous
rate proxy because this source does not supply a bootstrapped zero curve.

## Dollar-neutral nuke

The standalone `nuke` helper linearly rebases an observed bond quote from its
anchor stock and FX levels:

```text
B1 = B0 + delta * (S1 / FX1 - S0 / FX0).
```

FX follows the project convention of stock currency per bond currency, so
`S / FX` is the stock price in bond currency. Delta is frozen at the anchor and
is expressed in bond-price points per one-unit move in that converted stock
price. This preserves the anchor's volatility, rate, credit, and time context
only as a local approximation; it does not capture gamma or other repricing.


## Academic basis and limits

- [Cox, Ross, and Rubinstein (1979)](https://doi.org/10.1016/0304-405X(79)90015-1)
  provides the arbitrage-based recombining lattice and early-exercise method.
- [Ingersoll (1977)](https://doi.org/10.1016/0304-405X(77)90004-6) and
  [Brennan and Schwartz (1977)](https://doi.org/10.1111/j.1540-6261.1977.tb03364.x)
  establish contingent-claim CB valuation and optimal conversion/call logic.
- [Tsiveriotis and Fernandes (1998)](https://doi.org/10.3905/jfi.1998.408243)
  motivates separate risky cash and risk-free equity components.
- [Ayache, Forsyth, and Vetzal (2003)](https://doi.org/10.3905/jod.2003.319208)
  shows that TF does not explicitly specify the default event and can be
  internally inconsistent. Their reduced-form jump-to-default framework is the
  logical next model when recovery and default calibration are available.
- [Zabolotnyuk, Jones, and Veld (2010)](https://doi.org/10.1111/j.1755-053X.2010.01088.x)
  found similar out-of-sample mean absolute deviations for AFV (1.86%) and TF
  (1.94%), supporting TF as a practical baseline while its theoretical limits
  remain explicit.
- [Takahashi, Kobayashi, and Nakagawa (2001)](https://www.cirje.e.u-tokyo.ac.jp/research/dp/2001/2001cf140.pdf)
  gives a practical Duffie-Singleton-style alternative calibrated across the
  issuer's equity and debt.
- [Milanov and Kounchev (2012)](https://arxiv.org/abs/1111.2683) documents the
  convergence and Greek instability of a naive TF binomial implementation,
  which motivates explicit convergence tests and terminal smoothing here.

Limitations / areas for improvement:

- rates, spread, volatility, dividends, and borrow are flat over each run;
- coupon dates are maturity-anchored until explicit dates are normalized;
- yield calculations fall back to clean price plus inferred accrued interest
  when clean/dirty and day-count conventions are not normalized;
- a soft-call observation window is approximated by an instantaneous barrier;
- call notice periods, resets, dilution adjustments, and event puts are not
  fully modeled;
- cross-currency CBs use an effective one-factor stock conversion and omit FX
  volatility and stock/FX correlation;
- TF is a spread-discount model, not an explicit recovery/default model.
