##############################################################################
# Copyright (C) 2018, 2019, 2020 Dominic O'Kane
##############################################################################

import numpy as np
from numba import njit, float64, int64
from math import ceil

from ..finutils.FinError import FinError
from ..finutils.FinMath import accruedInterpolator
from ..market.curves.FinInterpolate import FinInterpMethods, uinterpolate
from ..finutils.FinHelperFunctions import labelToString

interp = FinInterpMethods.FLAT_FORWARDS.value

###############################################################################
# TODO : Calculate accrued in bond option according to accrual convention
# TODO : Convergence is unstable - investigate how to improve it
# TODO : Write a fallback for gradient based alpha using bisection
# TODO : Fix treatment of accrued interest on the option expiry date.
###############################################################################

# (c) Dominic O'Kane - December-2019
# Fergal O'Kane - search root function - 16-12-2019

###############################################################################

@njit(float64(float64, int64, float64[:], float64, float64, float64, int64),
      fastmath=True, cache=True)
def f(alpha, nm, Q, P, dX, dt, N):

    sumQZ = 0.0
    for j in range(-nm, nm+1):
        x = alpha + j*dX
        rdt = np.exp(x)*dt
        sumQZ += Q[j+N] * np.exp(-rdt)

    objFn = sumQZ - P
    return objFn

###############################################################################

@njit(float64(float64, int64, float64[:], float64, float64, float64, int64),
      fastmath=True, cache=True)
def fprime(alpha, nm, Q, P, dX, dt, N):

    sumQZdZ = 0.0
    for j in range(-nm, nm+1):
        x = alpha + j*dX
        rdt = np.exp(x)*dt
        sumQZdZ += Q[j+N] * np.exp(-rdt) * np.exp(x)

    deriv = -sumQZdZ*dt
    return deriv

###############################################################################
# This is the secant method which is not used as I computed the derivative of
# objective function with respect to the drift term
###############################################################################

@njit(float64(float64, int64, float64[:], float64, float64, float64, int64),
      fastmath=True, cache=True)
def searchRoot(x0, nm, Q, P, dX, dt, N):

#    print("Searching for root", x0)
    max_iter = 50
    max_error = 1e-8

    x1 = x0 * 1.0001
    f0 = f(x0, nm, Q, P, dX, dt, N)
    f1 = f(x1, nm, Q, P, dX, dt, N)

    for i in range(0, max_iter):

        df = f1 - f0

        if df == 0.0:
            raise FinError("Search for alpha fails due to zero derivative")

        x = x1 - f1 * (x1-x0)/df
        x0, f0 = x1, f1
        x1 = x
        f1 = f(x1, nm, Q, P, dX, dt, N)

        if (abs(f1) <= max_error):
            return x1

    raise FinError("Search root deriv FAILED to find alpha.")

###############################################################################
# This is Newton Raphson which is faster than the secant measure as it has the
# analytical derivative  that is easy to calculate.
# UPDATE IN MAY 2020: HAVING PROBLEMS WITH CONVERGENCE OF DERIVATIVE APPROACH
# DUE TO FLATNESS OF FUNCTION AT NEGATIVE X. NEEDED LOWER LIMIT NUMTIMESTEPS
###############################################################################

@njit(float64(float64, int64, float64[:], float64, float64, float64, int64),
      fastmath=True, cache=True)
def searchRootDeriv(x0, nm, Q, P, dX, dt, N):

    max_iter = 50
    max_error = 1e-7

    for i in range(0, max_iter):

        fval = f(x0, nm, Q, P, dX, dt, N)

        if abs(fval) <= max_error:
            return x0

        fderiv = fprime(x0, nm, Q, P, dX, dt, N)

        if abs(fderiv) == 0.0:
            raise FinError("Function derivative is zero.")

        step = fval/fderiv
        x0 = x0 - step

    raise FinError("Search root deriv FAILED to find alpha.")

###############################################################################


@njit(fastmath=True, cache=True)
def americanBondOption_Tree_Fast(texp, tmat, strikePrice,  face,
                                 couponTimes, couponFlows,
                                 americanExercise,
                                 _dfTimes, _dfValues,
                                 _treeTimes, _Q, _pu, _pm, _pd, _rt, _dt, _a):
    ''' Option that can be exercised at any time over the exercise period.
    Due to non-analytical bond price we need to extend tree out to bond
    maturity and take into account cash flows through time. '''

    DEBUG = False

    ###########################################################################

    numTimeSteps, numNodes = _Q.shape
    jmax = ceil(0.1835/(_a * _dt))
    expiryStep = int(texp/_dt + 0.50)
    maturityStep = int(tmat/_dt + 0.50)

    ###########################################################################

    treeFlows = np.zeros(numTimeSteps)
    numCoupons = len(couponTimes)

    if DEBUG:
        filename = "bondOptBK_" + str(numTimeSteps) + ".txt"
        outfile = open(filename, "w")

    # Tree flows go all the way out to the bond maturity date
    for i in range(0, numCoupons):
        tcpn = couponTimes[i]
        n = int(round(tcpn/_dt, 0))
        ttree = _treeTimes[n]
        df_flow = uinterpolate(tcpn, _dfTimes, _dfValues, interp)
        df_tree = uinterpolate(ttree, _dfTimes, _dfValues, interp)
        treeFlows[n] += couponFlows[i] * 1.0 * df_flow / df_tree

    if DEBUG:
        for i in range(0, len(_treeTimes)):
            t = _treeTimes[i]
            f = treeFlows[i]
            outfile.write("t: %9.5f  flow: %9.5f\n" % (t, f))

    ###########################################################################
    accrued = np.zeros(numTimeSteps)

    mappedTimes = [0.0]
    mappedAmounts = [0.0]
    for n in range(1, len(_treeTimes)):

        # I AM ENFORCING ZERO ACCRUED ON THE EXPIRY DATE OF THE OPTION
        # THIS NEEDS FURTHER WORK. I SHOULD CALCULATE THE EXPIRY ACCD EXACTLY
        # AND SET IT EQUAL TO THAT AMOUNT - !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

        accdAtExpiry = 0.0
        if _treeTimes[n-1] < texp and _treeTimes[n] >= texp:
            mappedTimes.append(texp)
            mappedAmounts.append(accdAtExpiry)

        if treeFlows[n] > 0.0:
            mappedTimes.append(_treeTimes[n])
            mappedAmounts.append(treeFlows[n])

    # I HAVE REMOVED THIS
    # Need cashflows which are exact time and size for accrued at texp
#    for n in range(0, numCoupons):
#        if couponTimes[n] > texp:
#            mappedTimes.append(couponTimes[n])
#            mappedAmounts.append(couponFlows[n])

    for m in range(0, maturityStep+1):
        ttree = _treeTimes[m]
        accrued[m] = accruedInterpolator(ttree, mappedTimes, mappedAmounts)
#        print("==>", m, ttree, accrued[m])
        accrued[m] *= face

        # This is a bit of a hack for when the interpolation does not put the
        # full accrued on flow date. Another scheme may work but so does this
        if treeFlows[m] > 0.0:
            accrued[m] = treeFlows[m] * face

    if DEBUG:
        print("TEXP", texp)
        print("TREE TIMES", _treeTimes)
        print("TREE FLOWS", treeFlows)
        print("MAPPED TIMES:", mappedTimes)
        print("MAPPED AMOUNTS:", mappedAmounts)
        print("ACCD TIMES", _treeTimes)
        print("ACCD AMOUNT", accrued)

    #######################################################################

    callOptionValues = np.zeros(shape=(numTimeSteps, numNodes))
    putOptionValues = np.zeros(shape=(numTimeSteps, numNodes))
    bondValues = np.zeros(shape=(numTimeSteps, numNodes))

    # Start with the value of the bond at maturity
    for k in range(0, numNodes):
        bondValues[maturityStep, k] = (1.0 + treeFlows[maturityStep]) * face

    if DEBUG:
        outfile.write("m: %d k: %d EXPIRY: %9.5f\n" %
                      (maturityStep, k, bondValues[maturityStep, k]))

    N = jmax

    for m in range(maturityStep-1, expiryStep-1, -1):

        nm = min(m, jmax)
        flow = treeFlows[m] * face

        for k in range(-nm, nm+1):
            kN = k + jmax
            df = np.exp(-_rt[m, kN] * _dt)
            pu = _pu[kN]
            pm = _pm[kN]
            pd = _pd[kN]

            if k == jmax:
                vu = bondValues[m+1, kN]
                vm = bondValues[m+1, kN-1]
                vd = bondValues[m+1, kN-2]
                v = (pu*vu + pm*vm + pd*vd) * df
                bondValues[m, kN] = v
            elif k == -jmax:
                vu = bondValues[m+1, kN+2]
                vm = bondValues[m+1, kN+1]
                vd = bondValues[m+1, kN]
                v = (pu*vu + pm*vm + pd*vd) * df
                bondValues[m, kN] = v
            else:
                vu = bondValues[m+1, kN+1]
                vm = bondValues[m+1, kN]
                vd = bondValues[m+1, kN-1]
                v = (pu*vu + pm*vm + pd*vd) * df
                bondValues[m, kN] = v

            bondValues[m, kN] += flow

        if DEBUG:
            outfile.write("m: %d k: %d flow: %9.5f BOND VALUE: %9.5f\n" %
                          (m, k, flow, bondValues[m, kN]))

    # Now consider exercise of the option on the expiry date
    # Start with the value of the bond at maturity and overwrite values
    nm = min(expiryStep, jmax)
    for k in range(-nm, nm+1):
        kN = k + N
        accd = accrued[expiryStep]
        cleanPrice = bondValues[expiryStep, kN] - accd
        callOptionValues[expiryStep, kN] = max(cleanPrice - strikePrice, 0.0)
        putOptionValues[expiryStep, kN] = max(strikePrice - cleanPrice, 0.0)

    if DEBUG:
        outfile.write("k: %d cleanPrice: %9.5f accd: %9.5f \n" % (k, cleanPrice, accd))

    if DEBUG:
        outfile.write("OPTION    m    k     treeT      treeF  bondVal   cleanPx     Accd    CallVal    PutVal\n")

    # Now step back to today considering early exercise
    for m in range(expiryStep-1, -1, -1):
        nm = min(m, jmax)
        flow = treeFlows[m] * face

        for k in range(-nm, nm+1):
            kN = k + N
            df = np.exp(-_rt[m, kN] * _dt)
            pu = _pu[kN]
            pm = _pm[kN]
            pd = _pd[kN]

            if k == jmax:
                vu = bondValues[m+1, kN]
                vm = bondValues[m+1, kN-1]
                vd = bondValues[m+1, kN-2]
                v = (pu*vu + pm*vm + pd*vd) * df
                bondValues[m, kN] = v
            elif k == jmax:
                vu = bondValues[m+1, kN+2]
                vm = bondValues[m+1, kN+1]
                vd = bondValues[m+1, kN]
                v = (pu*vu + pm*vm + pd*vd) * df
                bondValues[m, kN] = v
            else:
                vu = bondValues[m+1, kN+1]
                vm = bondValues[m+1, kN]
                vd = bondValues[m+1, kN-1]
                v = (pu*vu + pm*vm + pd*vd) * df
                bondValues[m, kN] = v

            bondValues[m, kN] += flow
            vcall = 0.0
            vput = 0.0

            if k == jmax:
                vu = callOptionValues[m+1, kN]
                vm = callOptionValues[m+1, kN-1]
                vd = callOptionValues[m+1, kN-2]
                vcall = (pu*vu + pm*vm + pd*vd) * df
            elif k == jmax:
                vu = callOptionValues[m+1, kN+2]
                vm = callOptionValues[m+1, kN+1]
                vd = callOptionValues[m+1, kN]
                vcall = (pu*vu + pm*vm + pd*vd) * df
            else:
                vu = callOptionValues[m+1, kN+1]
                vm = callOptionValues[m+1, kN]
                vd = callOptionValues[m+1, kN-1]
                vcall = (pu*vu + pm*vm + pd*vd) * df

            callOptionValues[m, kN] = vcall

            if k == jmax:
                vu = putOptionValues[m+1, kN]
                vm = putOptionValues[m+1, kN-1]
                vd = putOptionValues[m+1, kN-2]
                vput = (pu*vu + pm*vm + pd*vd) * df
            elif k == jmax:
                vu = putOptionValues[m+1, kN+2]
                vm = putOptionValues[m+1, kN+1]
                vd = putOptionValues[m+1, kN]
                vput = (pu*vu + pm*vm + pd*vd) * df
            else:
                vu = putOptionValues[m+1, kN+1]
                vm = putOptionValues[m+1, kN]
                vd = putOptionValues[m+1, kN-1]
                vput = (pu*vu + pm*vm + pd*vd) * df

            putOptionValues[m, kN] = vput

            if americanExercise is True:
                cleanPrice = bondValues[m, kN] - accrued[m]
                callExercise = max(cleanPrice - strikePrice, 0.0)
                putExercise = max(strikePrice - cleanPrice, 0.0)

                hold = callOptionValues[m, kN]
                callOptionValues[m, kN] = max(callExercise, hold)

                hold = putOptionValues[m, kN]
                putOptionValues[m, kN] = max(putExercise, hold)

        if DEBUG:
            outfile.write("CALL: %5d %5d %9.5f %9.5f %9.5f %9.5f %9.5f %9.5f %9.5f\n" % \
                    (m, k, _treeTimes[m], treeFlows[m], bondValues[m,kN],\
                     cleanPrice, accrued[m],\
                     callOptionValues[m,kN], putOptionValues[m,kN]))

    return callOptionValues[0, jmax], putOptionValues[0, jmax]

###############################################################################


@njit(fastmath=True, cache=True)
def callablePuttableBond_Tree_Fast(couponTimes, couponFlows,
                                   callTimes, callPrices,
                                   putTimes, putPrices, face,
                                   _sigma, _a, _Q,  # IS SIGMA USED ?
                                   _pu, _pm, _pd, _rt, _dt, _treeTimes,
                                   _dfTimes, _dfValues):
    ''' Value a bond with embedded put and call options that can be exercised
    at any time over the specified list of put and call dates.
    Due to non-analytical bond price we need to extend tree out to bond
    maturity and take into account cash flows through time. '''

    #######################################################################
    numTimeSteps, numNodes = _Q.shape
    dt = _dt
    jmax = ceil(0.1835/(_a * _dt))
    tmat = couponTimes[-1]
    maturityStep = int(tmat/dt + 0.50)

    ###########################################################################
    # Map coupons onto tree while preserving their present value
    ###########################################################################

    treeFlows = np.zeros(numTimeSteps)

    numCoupons = len(couponTimes)
    for i in range(0, numCoupons):
        tcpn = couponTimes[i]
        n = int(round(tcpn/_dt, 0))
        ttree = _treeTimes[n]
        df_flow = uinterpolate(tcpn, _dfTimes, _dfValues, interp)
        df_tree = uinterpolate(ttree, _dfTimes, _dfValues, interp)
        treeFlows[n] += couponFlows[i] * 1.0 * df_flow / df_tree

    #######################################################################
    # Mapped times stores the mapped times and flows and is used to calculate
    # accrued interest in a consistent manner as using actual flows will
    # result in some convergence noise issues as it is inconsistent
    #######################################################################

    mappedTimes = [0.0]
    mappedAmounts = [0.0]
    for n in range(1, len(_treeTimes)):
        if treeFlows[n] > 0.0:
            mappedTimes.append(_treeTimes[n])
            mappedAmounts.append(treeFlows[n])

    #######################################################################

    accrued = np.zeros(numTimeSteps)
    for m in range(0, numTimeSteps):
        ttree = _treeTimes[m]
        accrued[m] = accruedInterpolator(ttree, mappedTimes, mappedAmounts)
        accrued[m] *= face

        # This is a bit of a hack for when the interpolation does not put the
        # full accrued on flow date. Another scheme may work but so does this
        if treeFlows[m] > 0.0:
            accrued[m] = treeFlows[m] * face

    ###########################################################################
    # map call onto tree - must have no calls at high value
    ###########################################################################

    treeCallValue = np.ones(numTimeSteps) * face * 1000.0
    numCalls = len(callTimes)
    for i in range(0, numCalls):
        callTime = callTimes[i]
        n = int(round(callTime/dt, 0))
        treeCallValue[n] = callPrices[i]

    # map puts onto tree
    treePutValue = np.zeros(numTimeSteps)
    numPuts = len(putTimes)
    for i in range(0, numPuts):
        putTime = putTimes[i]
        n = int(round(putTime/dt, 0))
        treePutValue[n] = putPrices[i]

    ###########################################################################
    # Value the bond by backward induction starting at bond maturity
    ###########################################################################

    callPutBondValues = np.zeros(shape=(numTimeSteps, numNodes))
    bondValues = np.zeros(shape=(numTimeSteps, numNodes))

    DEBUG = False
    if DEBUG:
        df = 1.0
        px = 0.0
        for i in range(0, maturityStep+1):
            flow = treeFlows[i]
            t = _treeTimes[i]
            df = uinterpolate(t, _dfTimes, _dfValues, interp)
            px += flow*df
        px += df

    ###########################################################################
    # Now step back to today considering early exercise
    ###########################################################################

    m = maturityStep
    nm = min(maturityStep, jmax)
    vcall = treeCallValue[m]
    vput = treePutValue[m]
    vhold = (1.0 + treeFlows[m]) * face
    vclean = vhold - accrued[m]
    value = min(max(vclean, vput), vcall) + accrued[m]

    for k in range(-nm, nm+1):
        kN = k + jmax
        bondValues[m, kN] = (1.0 + treeFlows[m]) * face
        callPutBondValues[m, kN] = value

    for m in range(maturityStep-1, -1, -1):
        nm = min(m, jmax)
        flow = treeFlows[m] * face
        vcall = treeCallValue[m]
        vput = treePutValue[m]

        for k in range(-nm, nm+1):
            kN = k + jmax
            rt = _rt[m, kN]
            df = np.exp(-rt*dt)
            pu = _pu[kN]
            pm = _pm[kN]
            pd = _pd[kN]

            if k == jmax:
                vu = bondValues[m+1, kN]
                vm = bondValues[m+1, kN-1]
                vd = bondValues[m+1, kN-2]
            elif k == -jmax:
                vu = bondValues[m+1, kN+2]
                vm = bondValues[m+1, kN+1]
                vd = bondValues[m+1, kN]
            else:
                vu = bondValues[m+1, kN+1]
                vm = bondValues[m+1, kN]
                vd = bondValues[m+1, kN-1]

            v = (pu*vu + pm*vm + pd*vd) * df
            bondValues[m, kN] = v
            bondValues[m, kN] += flow

            if k == jmax:
                vu = callPutBondValues[m+1, kN]
                vm = callPutBondValues[m+1, kN-1]
                vd = callPutBondValues[m+1, kN-2]
            elif k == jmax:
                vu = callPutBondValues[m+1, kN+2]
                vm = callPutBondValues[m+1, kN+1]
                vd = callPutBondValues[m+1, kN]
            else:
                vu = callPutBondValues[m+1, kN+1]
                vm = callPutBondValues[m+1, kN]
                vd = callPutBondValues[m+1, kN-1]

            vhold = (pu*vu + pm*vm + pd*vd) * df
            # Need to make add on coupons paid if we hold
            vhold = vhold + flow
            value = min(max(vhold - accrued[m], vput), vcall) + accrued[m]
            callPutBondValues[m, kN] = value

    return {'bondwithoption': callPutBondValues[0, jmax],
            'bondpure': bondValues[0, jmax]}

###############################################################################


@njit(fastmath=True)
def buildTreeFast(a, sigma, treeTimes, numTimeSteps, discountFactors):

    treeMaturity = treeTimes[-1]
    dt = treeMaturity / (numTimeSteps+1)
    dX = sigma * np.sqrt(3.0 * dt)
    jmax = ceil(0.1835/(a * dt))
    jmin = - jmax

    if jmax > 1000:
        raise FinError("Jmax > 1000. Increase a or dt.")

    pu = np.zeros(shape=(2*jmax+1))
    pm = np.zeros(shape=(2*jmax+1))
    pd = np.zeros(shape=(2*jmax+1))

    # The short rate goes out one step extra to have the final short rate
    # This is the BK model so x = log(r)
    X = np.zeros(shape=(numTimeSteps+2, 2*jmax+1))
    rt = np.zeros(shape=(numTimeSteps+2, 2*jmax+1))

    # probabilities start at time 0 and go out to one step before T
    # Branching is simple trinomial out to time step m=1 after which
    # the top node and bottom node connect internally to two lower nodes
    # and two upper nodes respectively. The probabilities only depend on j

    for j in range(-jmax, jmax+1):
        ajdt = a*j*dt
        jN = j + jmax
        if j == jmax:
            pu[jN] = 7.0/6.0 + 0.50*(ajdt*ajdt - 3.0*ajdt)
            pm[jN] = -1.0/3.0 - ajdt*ajdt + 2.0*ajdt
            pd[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt - ajdt)
        elif j == -jmax:
            pu[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt + ajdt)
            pm[jN] = -1.0/3.0 - ajdt*ajdt - 2.0*ajdt
            pd[jN] = 7.0/6.0 + 0.50*(ajdt*ajdt + 3.0*ajdt)
        else:
            pu[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt - ajdt)
            pm[jN] = 2.0/3.0 - ajdt*ajdt
            pd[jN] = 1.0/6.0 + 0.50*(ajdt*ajdt + ajdt)

    # Arrow-Debreu array
    Q = np.zeros(shape=(numTimeSteps+2, 2*jmax+1))

    # This is the drift adjustment to ensure no arbitrage at each time
    alpha = np.zeros(numTimeSteps+1)

    # Time zero is trivial for the Arrow-Debreu price
    Q[0, jmax] = 1.0
    x0 = 3.0

    # Big loop over time steps
    for m in range(0, numTimeSteps + 1):

        nm = min(m, jmax)

        # Need to do drift adjustment which is non-linear and so requires
        # a root search algorithm - we NO LONGER choose to use Newton-Raphson
        # argtuple = (m, nm, Q[m], discountFactors[m+1], dX, dt, N)
        # MAY 2020 - DUE TO A NUMERICAL INSTABILITY IN FINDING THE ROOT I HAVE
        # LIMITED NUM TIME STEPS AS WHEN THESE ARE LARGE ROOT SEARCHING BREAKS

        alpha[m] = searchRootDeriv(x0, nm, Q[m],
                                   discountFactors[m+1], dX, dt, jmax)

        x0 = alpha[m]

        for j in range(-nm, nm+1):
            jN = j + jmax
            X[m, jN] = alpha[m] + j*dX
            rt[m, jN] = np.exp(X[m, jN])

        # Loop over all nodes at time m to calculate next values of Q
        for j in range(-nm, nm+1):
            jN = j + jmax
            rdt = np.exp(X[m, jN]) * dt
            z = np.exp(-rdt)

            if j == jmax:
                Q[m+1, jN] += Q[m, jN] * pu[jN] * z
                Q[m+1, jN-1] += Q[m, jN] * pm[jN] * z
                Q[m+1, jN-2] += Q[m, jN] * pd[jN] * z
            elif j == jmin:
                Q[m+1, jN] += Q[m, jN] * pd[jN] * z
                Q[m+1, jN+1] += Q[m, jN] * pm[jN] * z
                Q[m+1, jN+2] += Q[m, jN] * pu[jN] * z
            else:
                Q[m+1, jN+1] += Q[m, jN] * pu[jN] * z
                Q[m+1, jN] += Q[m, jN] * pm[jN] * z
                Q[m+1, jN-1] += Q[m, jN] * pd[jN] * z

    return (Q, pu, pm, pd, rt, dt)

##########################################################################


class FinModelRatesBK():

    def __init__(self, sigma, a, numTimeSteps=100):
        ''' Constructs the Black Karasinski rate model. The speed of mean
        reversion a and volatility are passed in. The short rate process
        is given by d(log(r)) = (theta(t) - a*log(r)) * dt  + sigma * dW '''

        if sigma < 0.0:
            raise FinError("Negative volatility not allowed.")

        if a < 0.0:
            raise FinError("Mean reversion speed parameter should be >= 0.")

        self._a = a
        self._sigma = sigma

        if numTimeSteps < 30:
            raise FinError("Drift fitting requires at least 30 time steps")

        self._numTimeSteps = numTimeSteps

        self._Q = None
        self._rt = None
        self._treeTimes = None
        self._pu = None
        self._pm = None
        self._pd = None
        self._discountCurve = None

###############################################################################

    def bondOption(self, texp, strike,
                   face, couponTimes, couponFlows, americanExercise):
        ''' Option that can be exercised at any time over the exercise period.
        Due to non-analytical bond price we need to extend tree out to bond
        maturity and take into account cash flows through time. '''

        tmat = couponTimes[-1]

        if texp > tmat:
            raise FinError("Option expiry after bond matures.")

        if texp < 0.0:
            raise FinError("Option expiry time negative.")

        #######################################################################

        callValue, putValue \
            = americanBondOption_Tree_Fast(texp, tmat, strike, face,
                                           couponTimes, couponFlows,
                                           americanExercise,
                                           self._dfTimes, self._dfValues,
                                           self._treeTimes, self._Q,
                                           self._pu, self._pm, self._pd,
                                           self._rt,
                                           self._dt, self._a)

        return {'call': callValue, 'put': putValue}

###############################################################################

    def callablePuttableBond_Tree(self,
                                  couponTimes, couponFlows,
                                  callTimes, callPrices,
                                  putTimes, putPrices,
                                  face):
        ''' Option that can be exercised at any time over the exercise period.
        Due to non-analytical bond price we need to extend tree out to bond
        maturity and take into account cash flows through time. '''

        callTimes = np.array(callTimes)
        putTimes = np.array(putTimes)

        callPrices = np.array(callPrices)
        putPrices = np.array(putPrices)

        v = callablePuttableBond_Tree_Fast(couponTimes, couponFlows,
                                           callTimes, callPrices,
                                           putTimes, putPrices, face,
                                           self._sigma, self._a,
                                           self._Q,
                                           self._pu, self._pm, self._pd,
                                           self._rt, self._dt,
                                           self._treeTimes,
                                           self._dfTimes, self._dfValues)

        return {'bondwithoption': v['bondwithoption'],
                'bondpure': v['bondpure']}

###############################################################################

    def buildTree(self, tmat, dfTimes, dfValues):

        interp = FinInterpMethods.FLAT_FORWARDS.value

        treeMaturity = tmat * (self._numTimeSteps+1)/self._numTimeSteps
        treeTimes = np.linspace(0.0, treeMaturity, self._numTimeSteps + 2)

        dfTree = np.zeros(shape=(self._numTimeSteps+2))
        dfTree[0] = 1.0

        for i in range(1, self._numTimeSteps+2):
            t = treeTimes[i]
            dfTree[i] = uinterpolate(t, dfTimes, dfValues, interp)

        self._Q, self._pu, self._pm, self._pd, self._rt, self._dt \
            = buildTreeFast(self._a, self._sigma,
                            treeTimes, self._numTimeSteps, dfTree)

        self._treeTimes = treeTimes
        self._dfTimes = dfTimes
        self._dfValues = dfValues

###############################################################################

    def __repr__(self):
        s = "Black-Karasinski Model\n"
        s += labelToString("Sigma", self._sigma)
        s += labelToString("a", self._a)
        return s

###############################################################################
