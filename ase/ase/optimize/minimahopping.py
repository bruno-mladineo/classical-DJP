import os
import numpy as np
from ase.io import read, write
from ase import io, units
from ase.optimize import QuasiNewton, FIRE, BFGS
from ase.md.npt import NPT
from ase.parallel import paropen, world
from ase.md import VelocityVerlet
from ase.md import MDLogger
from ase.md.velocitydistribution import MaxwellBoltzmannDistribution
from ase.ga.ofp_comparator import OFPComparator
from ase.calculators.lammpslib import is_upper_triangular, convert_cell_4NPT
from ase.constraints import Hookean, FixAtoms
from ase.constraints import ExpCellFilter

class MinimaHopping:
    """Implements the minima hopping method of global optimization outlined
    by S. Goedecker,  J. Chem. Phys. 120: 9911 (2004). Initialize with an
    ASE atoms object. Optional parameters are fed through keywords.
    To run multiple searches in parallel, specify the minima_traj keyword,
    and have each run point to the same path.
    """

    _default_settings = {
        'T0': 1000.,  # K, initial MD 'temperature'
        'beta1': 1.1,  # temperature adjustment parameter
        'beta2': 1.1,  # temperature adjustment parameter
        'beta3': 1. / 1.1,  # temperature adjustment parameter
        'Ediff0': 0.5,  # eV, initial energy acceptance threshold
        'alpha1': 0.98,  # energy threshold adjustment parameter
        'alpha2': 1. / 0.98,  # energy threshold adjustment parameter
        'mdmin': 2,  # criteria to stop MD simulation (no. of minima)
        'logfile': 'hop.log',  # text log
        'minima_threshold': 0.5e-3,  # A, cosine distance threshold for identical configs
        'ofp_rcut': 10.,  # A, cutoff for neighborlists for OFP comparison
        'ofp_sigma': 0.1, # A, variance of OFP gaussians
        'ofp_nsigma': 5,  # number of sigmas after OFP gaussians are cut
        'ofp_binwidth': 0.05, # binwidth for OFP gaussians discretization
        'timestep': 1.0,  # fs, timestep for MD simulations
        'optimizer': BFGS,  # local optimizer to use
        'minima_traj': 'minima.traj',  # storage file for minima list
        'fmax': 0.05,  # eV/A, max force for cell optimization
        'fmax2': 0.1,  # eV/A, max force for geometry optimization
        'initial_fmax': 0.05, # ev/A, max force for initial cell optimization
        'externalstress': 1e-1, # ev/A^3, the external stress tensor or scalar representing pressure.
        'ttime': 25.,  # fs, time constant for temperature coupling
        'pfactor': 0.6 * 75.**2, # constant in the barostat differential equation
        'k1': 15., # eV/A**2, spring constant for Hookean constraint between nearest neighbouring inorganic atoms
        'rt1': 0.01, # A, activate Hookean constraint if the bond between inorganic atoms lengthens for this amount
        'k2': 15., # eV/A**2, spring constant for Hookean constraint between pairs of Br atoms from different layers
        'rt2': 1.5, # A, activate Hookean constraint if the distance between pairs of Br atoms increases by this amount
        'constrain_bool': False} # should constraints be activated

    def __init__(self, atoms, **kwargs):
        """Initialize with an ASE atoms object and keyword arguments."""
        self._atoms = atoms
        for key in kwargs:
            if key not in self._default_settings:
                raise RuntimeError('Unknown keyword: %s' % key)
        for k, v in self._default_settings.items():
            setattr(self, '_%s' % k, kwargs.pop(k, v))

        # when a MD sim. has passed a local minimum:
        self._passedminimum = PassedMinimum()

        # Misc storage.
        self._minima_energies = []
        self._previous_optimum = None
        self._previous_energy = None
        self._temperature = self._T0
        self._Ediff = self._Ediff0

        #Oganov fingerprints for structure comparison
        self._comp = OFPComparator(dE=10.0,
                     cos_dist_max=self._minima_threshold, rcut=self._ofp_rcut, binwidth=self._ofp_binwidth,
                     pbc=[True, True, True], sigma=self._ofp_sigma, nsigma=self._ofp_nsigma,
                     recalculate=False)

        if self._constrain_bool == True:
            #inorganic indices and positions for Hookean constraints
            self._Pb_indices = np.where(self._atoms.symbols == 'Pb')[0]
            self._Pb_positions = self._atoms.positions[self._Pb_indices]
            self._Br_indices = np.where(self._atoms.symbols == 'Br')[0]
            self._Br_positions = self._atoms.positions[self._Br_indices]
            self._inorganic_indices = np.concatenate((self._Pb_indices, self._Br_indices))
            self._inorganic_positions = self._atoms.positions[self._inorganic_indices]
            #make list for distances of Pb atoms and 6 surrounding Br
#            self._distances_list = np.empty((len(self._Pb_indices), 6))
            self._indices_list = np.empty((len(self._Pb_indices), 6), dtype='int')

            for i in range(len(self._Pb_indices)):
                distances = self._atoms.get_distances(self._Pb_indices[i], self._Br_indices, mic=True)
#                self._distances_list[i] = distances[np.argsort(distances)[:6]]
                self._indices_list[i] = self._Br_indices[np.argsort(distances)[:6]]
        
            #find pairs of 2 Br's closest in z-direction and furthest in z-direction
            average_Br_z = np.average(self._Br_positions[:,2])
            top_Br = self._Br_indices[np.where(self._Br_positions[:,2] > average_Br_z)]
            bottom_Br = self._Br_indices[np.where(self._Br_positions[:,2] < average_Br_z)]
            #Br groups are labelled 1,2,3,4 from bottom to top so 1-4 and 2-3 should be paired
            Br_group1 = bottom_Br[np.argsort(self._atoms.positions[bottom_Br][:,2])[:4]]
            Br_group2 = bottom_Br[np.argsort(self._atoms.positions[bottom_Br][:,2])[-4:]]
            Br_group3 = top_Br[np.argsort(self._atoms.positions[top_Br][:,2])[:4]]
            Br_group4 = top_Br[np.argsort(self._atoms.positions[top_Br][:,2])[-4:]]
            
            self._Br_index1 = int(Br_group1[0])
            Br_relative1 = self._atoms.positions[self._Br_index1] - self._atoms.positions[Br_group4]
            self._Br_index4 = int(Br_group4[np.argmin(np.linalg.norm(Br_relative1[:,:2], axis=1))])
            
            Br_relative2 = self._atoms.positions[self._Br_index1] - self._atoms.positions[Br_group2]
            self._Br_index2 = int(Br_group2[np.argmin(np.linalg.norm(Br_relative2[:,:2], axis=1))])
            
            Br_relative3 = self._atoms.positions[self._Br_index2] - self._atoms.positions[Br_group3]
            self._Br_index3 = int(Br_group3[np.argmin(np.linalg.norm(Br_relative3[:,:2], axis=1))])

    def __call__(self, totalsteps=None, maxtemp=None):
        """Run the minima hopping algorithm. Can specify stopping criteria
        with total steps allowed or maximum searching temperature allowed.
        If neither is specified, runs indefinitely (or until stopped by
        batching software)."""
        self._startup()
        while True:
            if (totalsteps and self._counter >= totalsteps):
                self._log('msg', 'Run terminated. Step #%i reached of '
                          '%i allowed. Increase totalsteps if resuming.'
                          % (self._counter, totalsteps))
                return
            if (maxtemp and self._temperature >= maxtemp):
                self._log('msg', 'Run terminated. Temperature is %.2f K;'
                          ' max temperature allowed %.2f K.'
                          % (self._temperature, maxtemp))
                return

            self._previous_optimum = self._atoms.copy()
            self._previous_energy = self._atoms.get_potential_energy()
            if self._constrain_bool == True:
                self._constrain() #added by me
            self._molecular_dynamics()
            self._check_skew()
            self._optimize(self._fmax)
            self._counter += 1
            self._check_results()

    def _startup(self):
        """Initiates a run, and determines if running from previous data or
        a fresh run."""

        status = np.array(-1.)
        exists = self._read_minima()
        if world.rank == 0:
            if not exists:
                # Fresh run with new minima file.
                status = np.array(0.)
            elif not os.path.exists(self._logfile):
                # Fresh run with existing or shared minima file.
                status = np.array(1.)
            else:
                # Must be resuming from within a working directory.
                status = np.array(2.)
        world.barrier()
        world.broadcast(status, 0)

        if status == 2.:
            self._resume()
        else:
            self._counter = 0
            self._log('init')
            self._log('msg', 'Performing initial optimization.')
            if status == 1.:
                self._log('msg', 'Using existing minima file with %i prior '
                          'minima: %s' % (len(self._minima),
                                          self._minima_traj))
            if self._constrain_bool == True:
                self._constrain() #added by me
            self._optimize(self._initial_fmax)
            self._check_results()
            self._counter += 1

    def _resume(self):
        """Attempt to resume a run, based on information in the log
        file. Note it will almost always be interrupted in the middle of
        either a qn or md run or when exceeding totalsteps, so it only has
        been tested in those cases currently."""
        f = paropen(self._logfile, 'r')
        lines = f.read().splitlines()
        f.close()
        self._log('msg', 'Attempting to resume stopped run.')
        self._log('msg', 'Using existing minima file with %i prior '
                  'minima: %s' % (len(self._minima), self._minima_traj))
        mdcount, qncount = 0, 0
        for line in lines:
            if (line[:4] == 'par:') and ('Ediff' not in line):
                self._temperature = float(line.split()[1])
                self._Ediff = float(line.split()[2])
            elif line[:18] == 'msg: Optimization:':
                qncount = int(line[19:].split('qn')[1])
            elif line[:24] == 'msg: Molecular dynamics:':
                mdcount = int(line[25:].split('md')[1])
        self._counter = max((mdcount, qncount))
        if qncount == mdcount:
            # Either stopped during local optimization or terminated due to
            # max steps.
            self._log('msg', 'Attempting to resume at qn%05i' % qncount)
            if qncount > 0:
                atoms = io.read('qn%05i.traj' % (qncount - 1), index=-1)
                self._previous_optimum = atoms.copy()
                self._previous_energy = atoms.get_potential_energy()
            if os.path.getsize('qn%05i.traj' % qncount) > 0:
                atoms = io.read('qn%05i.traj' % qncount, index=-1)
            else:
                atoms = io.read('md%05i.traj' % qncount, index=-3)
            self._atoms.positions = atoms.get_positions()
            fmax = np.sqrt((atoms.get_forces() ** 2).sum(axis=1).max())
            if fmax < self._fmax:
                # Stopped after a qn finished.
                self._log('msg', 'qn%05i fmax already less than fmax=%.3f'
                          % (qncount, self._fmax))
                self._counter += 1
                return
            self._optimize()
            self._counter += 1
            if qncount > 0:
                self._check_results()
            else:
                self._record_minimum()
                self._log('msg', 'Found a new minimum.')
                self._log('msg', 'Accepted new minimum.')
                self._log('par')
        elif qncount < mdcount:
            # Probably stopped during molecular dynamics.
            self._log('msg', 'Attempting to resume at md%05i.' % mdcount)
            atoms = io.read('qn%05i.traj' % qncount, index=-1)
            self._previous_optimum = atoms.copy()
            self._previous_energy = atoms.get_potential_energy()
            self._molecular_dynamics(resume=mdcount)
            self._optimize()
            self._counter += 1
            self._check_results()

    def _check_results(self):
        """Adjusts parameters and positions based on outputs."""

        # No prior minima found?
        self._read_minima()
        if len(self._minima) == 0:
            self._log('msg', 'Found a new minimum.')
            self._log('msg', 'Accepted new minimum.')
            self._record_first_minimum()
            self._log('par')
            return

        # Energy and Oganov fingerprint minimum test

        if (self._previous_energy is None or
            (self._atoms.get_potential_energy() <
                self._previous_energy + self._Ediff)):
            unique, cosine_distances = self._is_unique()
            del self._atoms.info['fingerprints']
            if unique:
                        self._log('msg', 'Accepted new minimum.')
                        self._Ediff *= self._alpha1
                        self._temperature = self._T0
                        self._previous_optimum = self._atoms.copy()
                        self._log('par')
                        self._record_minimum()
            if not unique:
                        self._log('msg', 'Rejected minimum because a similar fingerprint was found.')

                        similar_index = np.argmin(cosine_distances)

                        if similar_index == len(self._minima) - 1 and self._atoms.get_potential_energy() < self._minima_energies[similar_index] and cosine_distances[-2] > self._minima_threshold and similar_index != 0:
                                    self._replace_minimum(similar_index)
                                    self._previous_optimum = self._atoms.copy()
                                    self._previous_energy = self._atoms.get_potential_energy()

                        if similar_index == 0:
                                    self._log('msg', 'Hop step returned to original structure. Resuming...')

                        self._atoms.positions = self._previous_optimum.positions
                        self._atoms.cell = self._previous_optimum.cell
                        self._Ediff *= self._alpha2
                        self._temperature *= self._beta1
                        self._log('par')
        else:
            self._log('msg', 'Rejected new minimum due to energy. '
                             'Restoring last minimum.')
            self._atoms.positions = self._previous_optimum.positions
            self._atoms.cell = self._previous_optimum.cell
            self._Ediff *= self._alpha2
            self._temperature *= self._beta1
            self._log('par')

    def _is_unique(self):
            cosine_distances = [self._comp._compare_structure(self._atoms, min) for min in self._minima]
            cosine_distances_string = ' '.join([str(elem) for elem in [round(elemo, 8) for elemo in cosine_distances]])

            self._log('msg', 'Cosine distances for candidate #%i = %s' % (self._counter - 1, cosine_distances_string))

            if True in [self._comp.looks_like(self._atoms, min) for min in self._minima]:
                        return False, cosine_distances
            else:
                        return True, cosine_distances

    def _log(self, cat='msg', message=None):
        """Records the message as a line in the log file."""
        if cat == 'init':
            if world.rank == 0:
                if os.path.exists(self._logfile):
                    raise RuntimeError('File exists: %s' % self._logfile)
            f = paropen(self._logfile, 'w')
            f.write('par: %12s %12s %12s\n' % ('T (K)', 'Ediff (eV)',
                                               'mdmin'))
            f.write('ene: %12s %12s %12s\n' % ('E_current', 'E_previous',
                                               'Difference'))
            f.close()
            return
        f = paropen(self._logfile, 'a')
        if cat == 'msg':
            line = 'msg: %s' % message
        elif cat == 'par':
            line = ('par: %12.4f %12.4f %12i' %
                    (self._temperature, self._Ediff, self._mdmin))
        elif cat == 'ene':
            current = self._atoms.get_potential_energy()
            if self._previous_optimum:
                previous = self._previous_energy
                line = ('ene: %12.5f %12.5f %12.5f' %
                        (current, previous, current - previous))
            else:
                line = ('ene: %12.5f' % current)
        f.write(line + '\n')
        f.close()

    def _optimize(self, cell_opt_fmax):
        """Perform an optimization."""
        if self._constrain_bool == True:
            del self._atoms.constraints[-2:]
        self._atoms.set_momenta(np.zeros(self._atoms.get_momenta().shape))
        geo_opt = FIRE(self._atoms)
        geo_opt.run(fmax=self._fmax2)
        ecf = ExpCellFilter(self._atoms)
        opt = self._optimizer(ecf,
                              trajectory='qn%05i.traj' % self._counter,
                              logfile='qn%05i.log' % self._counter)
        self._log('msg', 'Optimization: qn%05i' % self._counter)
        opt.run(fmax=cell_opt_fmax)
        self._check_skew()
        self._log('ene')
        if self._constrain_bool == True:
            del self._atoms.constraints
        tri_mat, coord_transform = convert_cell_4NPT(self._atoms.get_cell())
        self._atoms.set_positions([np.matmul(coord_transform, position) for position in self._atoms.get_positions()])
        self._atoms.set_cell(tri_mat.transpose())
        self._atoms.get_potential_energy()

    def _constrain(self):
        """Puts Hookean constraint on Pb atoms."""
#        self._Pb_positions = self._atoms.positions[self._Pb_indices]
#        self._Br_positions = self._atoms.positions[self._Br_indices]
        constraints_inorganic_Hookean = [Hookean(a1=int(self._Pb_indices[i]), a2=int(j), rt=self._atoms.get_distances(int(self._Pb_indices[i]), int(j), mic=True) + self._rt1, k=self._k1) for i in range(len(self._Pb_indices)) for j in self._indices_list[i]]
        constraint_Br1 = [Hookean(a1=self._Br_index1, a2=self._Br_index4, rt=self._atoms.get_distances(self._Br_index1, self._Br_index4, mic=True) + self._rt2, k=self._k2)]
        constraint_Br2 = [Hookean(a1=self._Br_index2, a2=self._Br_index3, rt=self._atoms.get_distances(self._Br_index2, self._Br_index3, mic=True) + self._rt2, k=self._k2)]

#        Pb_z = self._Pb_positions[:,2]
#        Pb_average_z = np.average(Pb_z)
#        Pb_upper_indices = self._Pb_indices[np.where(Pb_z > Pb_average_z)]
#        Pb_bottom_indices = self._Pb_indices[np.where(Pb_z < Pb_average_z)]
#        constraint_fix = [FixAtoms(indices=[Pb_upper_indices[0]])]
#        constraint_upper_to_fix = [Hookean(a1=Pb_upper_indices[0], a2=int(i), rt=self._atoms.get_distance(Pb_upper_indices[0], i, mic=True) + 0.1, k=5.) for i in Pb_upper_indices[1:]]
#        constraint_upper = [Hookean(a1=int(i), a2=int(j), rt=self._atoms.get_distance(i, j, mic=True) + 0.1, k=5.) for i in Pb_upper_indices[1:] for j in Pb_upper_indices[1:] if i < j]
#        constraint_bottom = [Hookean(a1=int(i), a2=int(j), rt=self._atoms.get_distance(i, j, mic=True) + 0.1, k=5.) for i in Pb_bottom_indices for j in Pb_bottom_indices if i < j]
        constraints_non_flat = [constrain for constrain in [constraints_inorganic_Hookean, constraint_Br1, constraint_Br2] if constrain != []]
        constraints = [item for sublist in constraints_non_flat for item in sublist]
#        constraints = [Hookean(a1=self._Pb_indices[i], a2=self._Pb_positions[i], rt=0.01, k=15.) for i in range(len(self._Pb_indices))]
#        self._atoms.set_constraint(constraints)
        self._atoms.set_constraint(constraints)
#        print(atoms.constraints)

    def _molecular_dynamics(self, resume=None):
        """Performs a molecular dynamics simulation, until mdmin is
        exceeded. If resuming, the file number (md%05i) is expected."""
        self._log('msg', 'Molecular dynamics: md%05i' % self._counter)
        mincount = 0
        energies, oldpositions = [], []
        thermalized = False
        if resume:
            self._log('msg', 'Resuming MD from md%05i.traj' % resume)
            if os.path.getsize('md%05i.traj' % resume) == 0:
                self._log('msg', 'md%05i.traj is empty. Resuming from '
                          'qn%05i.traj.' % (resume, resume - 1))
                atoms = io.read('qn%05i.traj' % (resume - 1), index=-1)
            else:
                images = io.Trajectory('md%05i.traj' % resume, 'r')
                for atoms in images:
                    energies.append(atoms.get_potential_energy())
                    oldpositions.append(atoms.positions.copy())
                    passedmin = self._passedminimum(energies)
                    if passedmin:
                        mincount += 1
                self._atoms.set_momenta(atoms.get_momenta())
                thermalized = True
            self._atoms.positions = atoms.get_positions()
            self._log('msg', 'Starting MD with %i existing energies.' %
                      len(energies))
        if not thermalized:
            MaxwellBoltzmannDistribution(self._atoms,
                                         temp=self._temperature * units.kB,
                                         force_temp=True)
        traj = io.Trajectory('md%05i.traj' % self._counter, 'a',
                             self._atoms)
#        if self._constrain_bool == True:
#            self._constrain()

        dyn = NPT(self._atoms, timestep=self._timestep * units.fs, temperature=self._temperature * units.kB, externalstress=self._externalstress, ttime=self._ttime*units.fs, pfactor=self._pfactor*units.fs**2)
#        dyn = NPTber(self._atoms, timestep=self._timestep * units.fs, temperature=self._temperature, fixcm=True, pressure=self._pressure, taut=self._taut * units.fs, taup=self._taup * units.fs, compressibility=self._compressibility)
        log = MDLogger(dyn, self._atoms, 'md%05i.log' % self._counter,
                       header=True, stress=False, peratom=False)
        dyn.attach(log, interval=1)
        dyn.attach(traj, interval=100)
        while mincount < self._mdmin:
            dyn.run(1)
            energies.append(self._atoms.get_potential_energy())
            passedmin = self._passedminimum(energies)
            if passedmin:
                mincount += 1
            oldpositions.append(self._atoms.positions.copy())
        # Reset atoms to minimum point.
        self._atoms.positions = oldpositions[passedmin[0]]

    def _check_skew(self):
        cell = np.copy(self._atoms.cell.standard_form()[0][:])
        if(np.abs(cell[2][1]) > 0.5 * cell[1][1] or np.abs(cell[2][0]) > 0.5 * cell[0][0] or np.abs(cell[1][0]) > 0.5 * cell[0][0]):
            self._log('msg', 'Cell skewed, unskewing.')
            self._unskew()

    def _unskew(self):
        self._atoms.set_cell(self._atoms.cell.standard_form()[0], scale_atoms=True)
        cell = self._atoms.cell[:]
        for i in range(2, 0, -1):
            for j in range(1, -1, -1):
                if (i > j):
                    while cell[i][j] > 0.5 * cell[j][j]:
                        cell[i] -= cell[j]
                    while cell[i][j] < -0.5 * cell[j][j]:
                        cell[i] += cell[j]
        self._atoms.set_cell(cell)
        self._atoms.wrap()
        tri_mat, coord_transform = convert_cell_4NPT(self._atoms.get_cell())
        self._atoms.set_positions([np.matmul(coord_transform, position) for position in self._atoms.get_positions()])
        self._atoms.set_cell(tri_mat.transpose())

    def _record_first_minimum(self):
        """Temporary kludge to write first minimum because otherwise it doesn't write its energy..."""
        write(self._minima_traj, self._atoms)
        self._minima_energies.append(self._atoms.get_potential_energy())
        self._read_minima()
        self._log('msg', 'Recorded minima #%i.' % (len(self._minima) - 1))

    def _record_minimum(self):
        """Adds the current atoms configuration to the minima list."""
        traj = io.Trajectory(self._minima_traj, 'a')
        traj.write(self._atoms)
        self._minima_energies.append(self._atoms.get_potential_energy())
        self._read_minima()
        self._log('msg', 'Recorded minima #%i.' % (len(self._minima) - 1))

    def _read_minima(self):
        """Reads in the list of minima from the minima file."""
        exists = os.path.exists(self._minima_traj)
        if exists:
            empty = os.path.getsize(self._minima_traj) == 0
            if not empty:
                traj = io.Trajectory(self._minima_traj, 'r')
                self._minima = [atoms for atoms in traj]
            else:
                self._minima = []
            return True
        else:
            self._minima = []
            return False

    def _replace_minimum(self, similar_index):
        """Replaces minimum that looks similar and has lower energy with current candidate."""
        minima_trajectory = read(self._minima_traj, index=':')
        minima_trajectory[similar_index] = self._atoms #.copy()
        self._minima_energies[similar_index] = self._atoms.get_potential_energy()
        write(self._minima_traj, minima_trajectory)
        self._read_minima()
        self._log('msg', 'Replaced minima #%i.' % similar_index)

class PassedMinimum:
    """Simple routine to find if a minimum in the potential energy surface
    has been passed. In its default settings, a minimum is found if the
    sequence ends with two downward points followed by two upward points.
    Initialize with n_down and n_up, integer values of the number of up and
    down points. If it has successfully determined it passed a minimum, it
    returns the value (energy) of that minimum and the number of positions
    back it occurred, otherwise returns None."""

    def __init__(self, n_down=2, n_up=2):
        self._ndown = n_down
        self._nup = n_up

    def __call__(self, energies):
        if len(energies) < (self._nup + self._ndown + 1):
            return None
        status = True
        index = -1
        for i_up in range(self._nup):
            if energies[index] < energies[index - 1]:
                status = False
            index -= 1
        for i_down in range(self._ndown):
            if energies[index] > energies[index - 1]:
                status = False
            index -= 1
        if status:
            return (-self._nup - 1), energies[-self._nup - 1]


class MHPlot:
    """Makes a plot summarizing the output of the MH algorithm from the
    specified rundirectory. If no rundirectory is supplied, uses the
    current directory."""

    def __init__(self, rundirectory=None, logname='hop.log'):
        if not rundirectory:
            rundirectory = os.getcwd()
        self._rundirectory = rundirectory
        self._logname = logname
        self._read_log()
        self._fig, self._ax = self._makecanvas()
        self._plot_data()

    def get_figure(self):
        """Returns the matplotlib figure object."""
        return self._fig

    def save_figure(self, filename):
        """Saves the file to the specified path, with any allowed
        matplotlib extension (e.g., .pdf, .png, etc.)."""
        self._fig.savefig(filename)

    def _read_log(self):
        """Reads relevant parts of the log file."""
        data = []  # format: [energy, status, temperature, ediff]
        f = open(os.path.join(self._rundirectory, self._logname), 'r')
        lines = f.read().splitlines()
        f.close()
        step_almost_over = False
        step_over = False
        for line in lines:
            if line.startswith('msg: Molecular dynamics:'):
                status = 'performing MD'
            elif line.startswith('msg: Optimization:'):
                status = 'performing QN'
            elif line.startswith('ene:'):
                status = 'local optimum reached'
                energy = floatornan(line.split()[1])
            elif line.startswith('msg: Accepted new minimum.'):
                status = 'accepted'
                step_almost_over = True
            elif line.startswith('msg: Found previously found minimum.'):
                status = 'previously found minimum'
                step_almost_over = True
            elif line.startswith('msg: Re-found last minimum.'):
                status = 'previous minimum'
                step_almost_over = True
            elif line.startswith('msg: Rejected new minimum'):
                status = 'rejected'
                step_almost_over = True
            elif line.startswith('par: '):
                temperature = floatornan(line.split()[1])
                ediff = floatornan(line.split()[2])
                if step_almost_over:
                    step_over = True
                    step_almost_over = False
            if step_over:
                data.append([energy, status, temperature, ediff])
                step_over = False
        if data[-1][1] != status:
            data.append([np.nan, status, temperature, ediff])
        self._data = data

    def _makecanvas(self):
        from matplotlib import pyplot
        from matplotlib.ticker import ScalarFormatter
        fig = pyplot.figure(figsize=(6., 8.))
        lm, rm, bm, tm = 0.22, 0.02, 0.05, 0.04
        vg1 = 0.01  # between adjacent energy plots
        vg2 = 0.03  # between different types of plots
        ratio = 2.  # size of an energy plot to a parameter plot
        figwidth = 1. - lm - rm
        totalfigheight = 1. - bm - tm - vg1 - 2. * vg2
        parfigheight = totalfigheight / (2. * ratio + 2)
        epotheight = ratio * parfigheight
        ax1 = fig.add_axes((lm, bm, figwidth, epotheight))
        ax2 = fig.add_axes((lm, bm + epotheight + vg1,
                            figwidth, epotheight))
        for ax in [ax1, ax2]:
            ax.yaxis.set_major_formatter(ScalarFormatter(useOffset=False))
        ediffax = fig.add_axes((lm, bm + 2. * epotheight + vg1 + vg2,
                                figwidth, parfigheight))
        tempax = fig.add_axes((lm, (bm + 2 * epotheight + vg1 + 2 * vg2 +
                                    parfigheight), figwidth, parfigheight))
        for ax in [ax2, tempax, ediffax]:
            ax.set_xticklabels([])
        ax1.set_xlabel('step')
        tempax.set_ylabel('$T$, K')
        ediffax.set_ylabel(r'$E_\mathrm{diff}$, eV')
        for ax in [ax1, ax2]:
            ax.set_ylabel(r'$E_\mathrm{pot}$, eV')
        ax = CombinedAxis(ax1, ax2, tempax, ediffax)
        self._set_zoomed_range(ax)
        ax1.spines['top'].set_visible(False)
        ax2.spines['bottom'].set_visible(False)
        return fig, ax

    def _set_zoomed_range(self, ax):
        """Try to intelligently set the range for the zoomed-in part of the
        graph."""
        energies = [line[0] for line in self._data
                    if not np.isnan(line[0])]
        dr = max(energies) - min(energies)
        if dr == 0.:
            dr = 1.
        ax.set_ax1_range((min(energies) - 0.2 * dr,
                          max(energies) + 0.2 * dr))

    def _plot_data(self):
        for step, line in enumerate(self._data):
            self._plot_energy(step, line)
            self._plot_qn(step, line)
            self._plot_md(step, line)
        self._plot_parameters()
        self._ax.set_xlim(self._ax.ax1.get_xlim())

    def _plot_energy(self, step, line):
        """Plots energy and annotation for acceptance."""
        energy, status = line[0], line[1]
        if np.isnan(energy):
            return
        self._ax.plot([step, step + 0.5], [energy] * 2, '-',
                      color='k', linewidth=2.)
        if status == 'accepted':
            self._ax.text(step + 0.51, energy, r'$\checkmark$')
        elif status == 'rejected':
            self._ax.text(step + 0.51, energy, r'$\Uparrow$', color='red')
        elif status == 'previously found minimum':
            self._ax.text(step + 0.51, energy, r'$\hookleftarrow$',
                          color='red', va='center')
        elif status == 'previous minimum':
            self._ax.text(step + 0.51, energy, r'$\leftarrow$',
                          color='red', va='center')

    def _plot_md(self, step, line):
        """Adds a curved plot of molecular dynamics trajectory."""
        if step == 0:
            return
        energies = [self._data[step - 1][0]]
        file = os.path.join(self._rundirectory, 'md%05i.traj' % step)
        traj = io.Trajectory(file, 'r')
        for atoms in traj:
            energies.append(atoms.get_potential_energy())
        xi = step - 1 + .5
        if len(energies) > 2:
            xf = xi + (step + 0.25 - xi) * len(energies) / (len(energies) - 2.)
        else:
            xf = step
        if xf > (step + .75):
            xf = step
        self._ax.plot(np.linspace(xi, xf, num=len(energies)), energies,
                      '-k')

    def _plot_qn(self, index, line):
        """Plots a dashed vertical line for the optimization."""
        if line[1] == 'performing MD':
            return
        file = os.path.join(self._rundirectory, 'qn%05i.traj' % index)
        if os.path.getsize(file) == 0:
            return
        traj = io.Trajectory(file, 'r')
        energies = [traj[0].get_potential_energy(),
                    traj[-1].get_potential_energy()]
        if index > 0:
            file = os.path.join(self._rundirectory, 'md%05i.traj' % index)
            atoms = io.read(file, index=-3)
            energies[0] = atoms.get_potential_energy()
        self._ax.plot([index + 0.25] * 2, energies, ':k')

    def _plot_parameters(self):
        """Adds a plot of temperature and Ediff to the plot."""
        steps, Ts, ediffs = [], [], []
        for step, line in enumerate(self._data):
            steps.extend([step + 0.5, step + 1.5])
            Ts.extend([line[2]] * 2)
            ediffs.extend([line[3]] * 2)
        self._ax.tempax.plot(steps, Ts)
        self._ax.ediffax.plot(steps, ediffs)

        for ax in [self._ax.tempax, self._ax.ediffax]:
            ylim = ax.get_ylim()
            yrange = ylim[1] - ylim[0]
            ax.set_ylim((ylim[0] - 0.1 * yrange, ylim[1] + 0.1 * yrange))


def floatornan(value):
    """Converts the argument into a float if possible, np.nan if not."""
    try:
        output = float(value)
    except ValueError:
        output = np.nan
    return output


class CombinedAxis:
    """Helper class for MHPlot to plot on split y axis and adjust limits
    simultaneously."""

    def __init__(self, ax1, ax2, tempax, ediffax):
        self.ax1 = ax1
        self.ax2 = ax2
        self.tempax = tempax
        self.ediffax = ediffax
        self._ymax = -np.inf

    def set_ax1_range(self, ylim):
        self._ax1_ylim = ylim
        self.ax1.set_ylim(ylim)

    def plot(self, *args, **kwargs):
        self.ax1.plot(*args, **kwargs)
        self.ax2.plot(*args, **kwargs)
        # Re-adjust yrange
        for yvalue in args[1]:
            if yvalue > self._ymax:
                self._ymax = yvalue
        self.ax1.set_ylim(self._ax1_ylim)
        self.ax2.set_ylim((self._ax1_ylim[1], self._ymax))

    def set_xlim(self, *args):
        self.ax1.set_xlim(*args)
        self.ax2.set_xlim(*args)
        self.tempax.set_xlim(*args)
        self.ediffax.set_xlim(*args)

    def text(self, *args, **kwargs):
        y = args[1]
        if y < self._ax1_ylim[1]:
            ax = self.ax1
        else:
            ax = self.ax2
        ax.text(*args, **kwargs)
