import numpy
from pyscf.dft import numint
from pyscf.dft.numint import SWITCH_SIZE
from pyscf.dft.gen_grid import make_mask, BLKSIZE
from pyscfad.lib import numpy as jnp
from pyscfad.lib import ops
from . import libxc

def nr_rks(ni, mol, grids, xc_code, dms, relativity=0, hermi=0,
           max_memory=2000, verbose=None):
    xctype = ni._xc_type(xc_code)
    make_rho, nset, nao = ni._gen_rho_evaluator(mol, dms, hermi)

    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()

    nelec = numpy.zeros(nset)
    excsum = jnp.zeros(nset)
    if hasattr(dms, "ndim"):
        vmat = jnp.zeros((nset,nao,nao), dtype=dms.dtype)
    else:
        vmat = jnp.zeros((nset,nao,nao), dtype=jnp.result_type(*dms))
    aow = None
    if xctype == 'LDA':
        ao_deriv = 0
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao.shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'LDA')
                exc, vxc = ni.eval_xc(xc_code, rho, spin=0,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]
                vrho = vxc[0]
                den = rho * weight
                nelec[idm] += getattr(den.sum(), "val", den.sum())
                excsum = ops.index_add(excsum, ops.index[idm], jnp.dot(den, exc))
                # *.5 because vmat + vmat.T
                #:aow = numpy.einsum('pi,p->pi', ao, .5*weight*vrho, out=aow)
                aow = _scale_ao(ao, .5*weight*vrho, out=None)
                vmat = ops.index_add(vmat, ops.index[idm], _dot_ao_ao(mol, ao, aow, mask, shls_slice, ao_loc))
                rho = exc = vxc = vrho = None
    elif xctype == 'GGA':
        ao_deriv = 1
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            #aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'GGA')
                exc, vxc = ni.eval_xc(xc_code, rho, spin=0,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]
                den = rho[0] * weight
                nelec[idm] += getattr(den.sum(), "val", den.sum())
                excsum = ops.index_add(excsum, ops.index[idm], jnp.dot(den, exc))
                # ref eval_mat function
                wv = _rks_gga_wv0(rho, vxc, weight)
                #:aow = numpy.einsum('npi,np->pi', ao, wv, out=aow)
                aow = _scale_ao(ao, wv, out=None)
                vmat = ops.index_add(vmat, ops.index[idm], _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc))
                rho = exc = vxc = wv = None
    elif xctype == 'NLC':
        nlc_pars = ni.nlc_coeff(xc_code[:-6])
        if nlc_pars == [0,0]:
            raise NotImplementedError('VV10 cannot be used with %s. '
                                      'The supported functionals are %s' %
                                      (xc_code[:-6], ni.libxc.VV10_XC))
        ao_deriv = 1
        vvrho=numpy.empty([nset,4,0])
        vvweight=numpy.empty([nset,0])
        vvcoords=numpy.empty([nset,0,3])
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            rhotmp = numpy.empty([0,4,weight.size])
            weighttmp = numpy.empty([0,weight.size])
            coordstmp = numpy.empty([0,weight.size,3])
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'GGA')
                rho = numpy.expand_dims(rho,axis=0)
                rhotmp = numpy.concatenate((rhotmp,rho),axis=0)
                weighttmp = numpy.concatenate((weighttmp,numpy.expand_dims(weight,axis=0)),axis=0)
                coordstmp = numpy.concatenate((coordstmp,numpy.expand_dims(coords,axis=0)),axis=0)
                rho = None
            vvrho=numpy.concatenate((vvrho,rhotmp),axis=2)
            vvweight=numpy.concatenate((vvweight,weighttmp),axis=1)
            vvcoords=numpy.concatenate((vvcoords,coordstmp),axis=1)
            rhotmp = weighttmp = coordstmp = None
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'GGA')
                exc, vxc = _vv10nlc(rho,coords,vvrho[idm],vvweight[idm],vvcoords[idm],nlc_pars)
                den = rho[0] * weight
                nelec[idm] += den.sum()
                excsum[idm] += numpy.dot(den, exc)
# ref eval_mat function
                wv = _rks_gga_wv0(rho, vxc, weight)
                #:aow = numpy.einsum('npi,np->pi', ao, wv, out=aow)
                aow = _scale_ao(ao, wv, out=aow)
                vmat[idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)
                rho = exc = vxc = wv = None
        vvrho = vvweight = vvcoords = None
    elif xctype == 'MGGA':
        if (any(x in xc_code.upper() for x in ('CC06', 'CS', 'BR89', 'MK00'))):
            raise NotImplementedError('laplacian in meta-GGA method')
        ao_deriv = 2
        for ao, mask, weight, coords \
                in ni.block_loop(mol, grids, nao, ao_deriv, max_memory):
            aow = numpy.ndarray(ao[0].shape, order='F', buffer=aow)
            for idm in range(nset):
                rho = make_rho(idm, ao, mask, 'MGGA')
                exc, vxc = ni.eval_xc(xc_code, rho, spin=0,
                                      relativity=relativity, deriv=1,
                                      verbose=verbose)[:2]
                vrho, vsigma, vlapl, vtau = vxc[:4]
                den = rho[0] * weight
                nelec[idm] += den.sum()
                excsum[idm] += numpy.dot(den, exc)

                wv = _rks_gga_wv0(rho, vxc, weight)
                #:aow = numpy.einsum('npi,np->pi', ao[:4], wv, out=aow)
                aow = _scale_ao(ao[:4], wv, out=aow)
                vmat[idm] += _dot_ao_ao(mol, ao[0], aow, mask, shls_slice, ao_loc)

# FIXME: .5 * .5   First 0.5 for v+v.T symmetrization.
# Second 0.5 is due to the Libxc convention tau = 1/2 \nabla\phi\dot\nabla\phi
                wv = (.5 * .5 * weight * vtau).reshape(-1,1)
                vmat[idm] += _dot_ao_ao(mol, ao[1], wv*ao[1], mask, shls_slice, ao_loc)
                vmat[idm] += _dot_ao_ao(mol, ao[2], wv*ao[2], mask, shls_slice, ao_loc)
                vmat[idm] += _dot_ao_ao(mol, ao[3], wv*ao[3], mask, shls_slice, ao_loc)

                rho = exc = vxc = vrho = wv = None

    #for i in range(nset):
    #    vmat[i] = vmat[i] + vmat[i].conj().T
    vmat = vmat + vmat.conj().transpose(0,2,1)
    if nset == 1:
        nelec = nelec[0]
        excsum = excsum[0]
        vmat = vmat[0]
    return nelec, excsum, vmat

def eval_rho(mol, ao, dm, non0tab=None, xctype='LDA', hermi=0, verbose=None):
    xctype = xctype.upper()
    if xctype == 'LDA' or xctype == 'HF':
        ngrids, nao = ao.shape
    else:
        ngrids, nao = ao[0].shape

    if non0tab is None:
        non0tab = numpy.ones(((ngrids+BLKSIZE-1)//BLKSIZE,mol.nbas),
                             dtype=numpy.uint8)
    if not hermi:
        # (D + D.T)/2 because eval_rho computes 2*(|\nabla i> D_ij <j|) instead of
        # |\nabla i> D_ij <j| + |i> D_ij <\nabla j| for efficiency
        dm = (dm + dm.conj().T) * .5

    shls_slice = (0, mol.nbas)
    ao_loc = mol.ao_loc_nr()
    if xctype == 'LDA' or xctype == 'HF':
        c0 = _dot_ao_dm(mol, ao, dm, non0tab, shls_slice, ao_loc)
        #:rho = numpy.einsum('pi,pi->p', ao, c0)
        rho = _contract_rho(ao, c0)
    elif xctype in ('GGA', 'NLC'):
        rho = jnp.empty((4,ngrids))
        c0 = _dot_ao_dm(mol, ao[0], dm, non0tab, shls_slice, ao_loc)
        #:rho[0] = numpy.einsum('pi,pi->p', c0, ao[0])
        rho = ops.index_update(rho, ops.index[0], _contract_rho(c0, ao[0]))
        for i in range(1, 4):
            #:rho[i] = numpy.einsum('pi,pi->p', c0, ao[i])
            rho = ops.index_update(rho, ops.index[i], _contract_rho(c0, ao[i]) * 2)
    else: # meta-GGA
        # rho[4] = \nabla^2 rho, rho[5] = 1/2 |nabla f|^2
        rho = jnp.empty((6,ngrids))
        c0 = _dot_ao_dm(mol, ao[0], dm, non0tab, shls_slice, ao_loc)
        #:rho[0] = numpy.einsum('pi,pi->p', ao[0], c0)
        rho = ops.index_update(rho, ops.index[0], _contract_rho(ao[0], c0))
        rho = ops.index_update(rho, ops.index[5], 0)
        for i in range(1, 4):
            #:rho[i] = numpy.einsum('pi,pi->p', c0, ao[i]) * 2 # *2 for +c.c.
            rho = ops.index_update(rho, ops.index[i], _contract_rho(c0, ao[i]) * 2)
            c1 = _dot_ao_dm(mol, ao[i], dm.T, non0tab, shls_slice, ao_loc)
            #:rho[5] += numpy.einsum('pi,pi->p', c1, ao[i])
            rho = ops.index_add(rho, ops.index[5], _contract_rho(c1, ao[i]))
        XX, YY, ZZ = 4, 7, 9
        ao2 = ao[XX] + ao[YY] + ao[ZZ]
        #:rho[4] = numpy.einsum('pi,pi->p', c0, ao2)
        rho = ops.index_update(rho, ops.index[4], _contract_rho(c0, ao2))
        rho = ops.index_add(rho, ops.index[4], rho[5])
        rho = ops.index_mul(rho, ops.index[4], 2)
        rho = ops.index_mul(rho, ops.index[5], .5)
    return rho

def _scale_ao(ao, wv, out=None):
    #:aow = numpy.einsum('npi,np->pi', ao[:4], wv)
    if wv.ndim == 2:
        ao = ao.transpose(0,2,1)
    else:
        ngrids, nao = ao.shape
        ao = ao.T.reshape(1,nao,ngrids)
        wv = wv.reshape(1,ngrids)

    aow = jnp.einsum('nip,np->pi', ao, wv)
    return aow

def _dot_ao_ao(mol, ao1, ao2, non0tab, shls_slice, ao_loc, hermi=0):
    '''return numpy.dot(ao1.T, ao2)'''
    ngrids, nao = ao1.shape
    if nao < SWITCH_SIZE:
        return jnp.dot(ao1.T.conj(), ao2)

def _dot_ao_dm(mol, ao, dm, non0tab, shls_slice, ao_loc, out=None):
    '''return numpy.dot(ao, dm)'''
    ngrids, nao = ao.shape
    if nao < SWITCH_SIZE:
        return jnp.dot(jnp.asarray(dm).T, ao.T).T
    else:
        raise NotImplementedError

def _contract_rho(bra, ket):
    bra = bra.T
    ket = ket.T
    nao, ngrids = bra.shape

    rho  = jnp.einsum('ip,ip->p', bra.real, ket.real)
    rho += jnp.einsum('ip,ip->p', bra.imag, ket.imag)
    return rho

def _rks_gga_wv0(rho, vxc, weight):
    vrho, vgamma = vxc[:2]
    ngrid = vrho.size
    wv = jnp.empty((4,ngrid))
    wv = ops.index_update(wv, ops.index[0], weight * vrho * .5)
    wv = ops.index_update(wv, ops.index[1:], (weight * vgamma * 2) * rho[1:4])
    #wv = ops.index_mul(wv, ops.index[0], .5)  # v+v.T should be applied in the caller
    return wv

class NumInt(numint.NumInt):
    def _gen_rho_evaluator(self, mol, dms, hermi=0):
        if getattr(dms, 'mo_coeff', None) is not None:
            #TODO: test whether dm.mo_coeff matching dm
            mo_coeff = dms.mo_coeff
            mo_occ = dms.mo_occ
            if isinstance(dms, numpy.ndarray) and dms.ndim == 2:
                mo_coeff = [mo_coeff]
                mo_occ = [mo_occ]
            nao = mo_coeff[0].shape[0]
            ndms = len(mo_occ)
            def make_rho(idm, ao, non0tab, xctype):
                return self.eval_rho2(mol, ao, mo_coeff[idm], mo_occ[idm],
                                      non0tab, xctype)
        else:
            if getattr(dms, "ndim", None) == 2:
                dms = [dms]
            if not hermi:
                # For eval_rho when xctype==GGA, which requires hermitian DMs
                dms = [(dm+dm.conj().T)*.5 for dm in dms]
            nao = dms[0].shape[0]
            ndms = len(dms)
            def make_rho(idm, ao, non0tab, xctype):
                return self.eval_rho(mol, ao, dms[idm], non0tab, xctype, hermi=1)
        return make_rho, ndms, nao

    def eval_xc(self, xc_code, rho, spin=0, relativity=0, deriv=1, omega=None,
                verbose=None):
        if omega is None: omega = self.omega
        return libxc.eval_xc(xc_code, rho, spin, relativity, deriv,
                             omega, verbose)

    def eval_rho(self, mol, ao, dm, non0tab=None, xctype='LDA', hermi=0, verbose=None):
        return eval_rho(mol, ao, dm, non0tab, xctype, hermi, verbose)

    nr_rks = nr_rks
