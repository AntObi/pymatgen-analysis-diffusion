# coding: utf-8
# Copyright (c) Materials Virtual Lab.
# Distributed under the terms of the BSD License.
"""
Created on April 01, 2019
"""

__author__ = "Jimmy Shen"
__copyright__ = "Copyright 2019, The Materials Project"
__version__ = "0.1"
__maintainer__ = "Jimmy Shen"
__email__ = "jmmshn@lbl.gov"
__date__ = "April 11, 2019"

from collections import defaultdict
from copy import deepcopy
import logging
from typing import Union, List, Dict

from pymatgen_diffusion.neb.periodic_dijkstra import (
    periodic_dijkstra,
    get_optimal_pathway_rev,
)
from pymatgen.io.vasp import VolumetricData
from pymatgen.core.structure import Composition
from pymatgen.analysis.structure_matcher import StructureMatcher, ElementComparator
from pymatgen.analysis.graphs import StructureGraph
from pymatgen.core import Structure, PeriodicSite
from pymatgen.core.periodic_table import get_el_sp
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer
from pymatgen.analysis.local_env import MinimumDistanceNN
import operator
import numpy as np
import networkx as nx
from itertools import starmap
from pymatgen_diffusion.neb.pathfinder import MigrationPath
from monty.json import MSONable
from pymatgen.analysis.path_finder import NEBPathfinder, ChgcarPotential
import copy
from typing import Callable

logger = logging.getLogger(__name__)

# Magic Numbers
BASE_COLLISION_R = (
    1.0  # Eliminate cation sites that are too close to the sites in the base structure
)
SITE_MERGE_R = 1.0  # Merge cation sites that are too close together


def generic_groupby(list_in: list, comp: Callable = operator.eq):
    """
    Group a list of unsortable objects

    Args:
        list_in: A list of generic objects
        comp: (Default value = operator.eq) The comparator

    Returns:
        [int] list of labels for the input list

    """
    list_out = [None] * len(list_in)
    label_num = 0
    for i1, ls1 in enumerate(list_out):
        if ls1 is not None:
            continue
        list_out[i1] = label_num
        for i2, ls2 in list(enumerate(list_out))[i1 + 1 :]:
            if comp(list_in[i1], list_in[i2]):
                if list_out[i2] is None:
                    list_out[i2] = list_out[i1]
                else:
                    list_out[i1] = list_out[i2]
                    label_num -= 1
        label_num += 1
    return list_out


class FullPathMapper(MSONable):
    """
    Find all hops in a given crystal structure using the StructureGraph.
    Each hop is an edge in the StructureGraph object and each node is a position of the migrating species in the
    structure
    The equivalence of the hops is checked using the MigrationPath.__eq__ function.
    The functions here are responsible for distinguishing the individual hops and analysis
    """

    def __init__(
        self,
        structure,
        migrating_specie,
        max_path_length=10,
        symprec=0.1,
        vac_mode=False,
        name: str = None,
    ):
        """
        Args:
            structure: Input structure that contains all sites.
            migrating_specie (Specie-like): The specie that migrates. E.g.,
                "Li".
            max_path_length (float): Maximum length of NEB path in the unit
                of Angstrom. Defaults to None, which means you are setting the
                value to the min cutoff until finding 1D or >1D percolating paths.
            symprec (float): Symmetry precision to determine equivalence.
        """
        self.structure = structure
        self.migrating_specie = get_el_sp(migrating_specie)
        self.max_path_length = max_path_length
        self.symprec = symprec
        self.name = name
        self.a = SpacegroupAnalyzer(self.structure, symprec=self.symprec)
        self.symm_structure = self.a.get_symmetrized_structure()
        self.only_sites = self.get_only_sites()
        if vac_mode:
            raise NotImplementedError
        self.vac_mode = vac_mode
        self.unique_hops = None

        # Generate the graph edges between these all the sites
        self.s_graph = StructureGraph.with_local_env_strategy(
            self.only_sites,
            MinimumDistanceNN(cutoff=max_path_length, get_all_sites=True),
        )  # weights in this graph are the distances
        self.s_graph.set_node_attributes()
        self.populate_edges_with_migration_paths()
        self.group_and_label_hops()
        self._populate_unique_hops_dict()

    # TODO add classmethod for creating the FullPathMapper from the charge density

    def get_only_sites(self):
        """
        Get a copy of the structure with only the sites

        Args:

        Returns:
          Structure: Structure with all possible migrating ion sites

        """
        migrating_ion_sites = list(
            filter(
                lambda site: site.species == Composition({self.migrating_specie: 1}),
                self.structure.sites,
            )
        )
        return Structure.from_sites(migrating_ion_sites)

    def _get_pos_and_migration_path(self, u, v, w):
        """
        insert a single MigrationPath object on a graph edge
        Args:
          u (int): index of initial node
          v (int): index of final node
          w (int): index for multiple edges that share the same two nodes

        """
        edge = self.s_graph.graph[u][v][w]
        i_site = self.only_sites.sites[u]
        f_site = PeriodicSite(
            self.only_sites.sites[v].species,
            self.only_sites.sites[v].frac_coords + np.array(edge["to_jimage"]),
            lattice=self.only_sites.lattice,
        )
        # Positions might be useful for plotting
        edge["ipos"] = i_site.frac_coords
        edge["epos"] = f_site.frac_coords
        edge["ipos_cart"] = np.dot(i_site.frac_coords, self.only_sites.lattice.matrix)
        edge["epos_cart"] = np.dot(f_site.frac_coords, self.only_sites.lattice.matrix)

        edge["hop"] = MigrationPath(i_site, f_site, self.symm_structure)

    def populate_edges_with_migration_paths(self):
        """
        Populate the edges with the data for the Migration Paths
        """
        list(starmap(self._get_pos_and_migration_path, self.s_graph.graph.edges))

    def group_and_label_hops(self):
        """
        Group the MigrationPath objects together and label all the symmetrically equlivaelnt hops with the same label
        """
        hops = [
            (g_index, val)
            for g_index, val in nx.get_edge_attributes(
                self.s_graph.graph, "hop"
            ).items()
        ]
        labs = generic_groupby(hops, comp=lambda x, y: x[1] == y[1])
        new_attr = {
            g_index: {"hop_label": labs[edge_index]}
            for edge_index, (g_index, _) in enumerate(hops)
        }
        nx.set_edge_attributes(self.s_graph.graph, new_attr)
        return new_attr

    def _populate_unique_hops_dict(self):
        """
        Populate the unique hops
        """
        # reversed so that the first instance represents the group of distinct hops
        ihop_data = list(reversed(list(self.s_graph.graph.edges(data=True))))
        for u, v, d in ihop_data:
            d["iindex"] = u
            d["eindex"] = v
            d["hop_distance"] = d["hop"].length
        self.unique_hops = {d["hop_label"]: d for u, v, d in ihop_data}

    def add_data_to_similar_edges(
        self, target_label: Union[int, str], data: dict, m_path: MigrationPath = None
    ):
        """
        Insert data to all edges with the same label

        Args:
            target_label: The edge uniqueness label are adding data
            data: The data to passed to the different edges
            m_path: If the data is an array, and m_path is set, it uses the reference migration path to
            determine whether the data needs to be flipped so that 0-->1 is different from 1-->0
        """

        for u, v, d in self.s_graph.graph.edges(data=True):
            if d["hop_label"] == target_label:
                d.update(data)
                if m_path is not None:
                    # Try to override the data.
                    if not m_path.symm_structure.spacegroup.are_symmetrically_equivalent(
                        [m_path.isite], [d["hop"].isite]
                    ):
                        # "The data going to this edge needs to be flipped"
                        for k in data.keys():
                            if isinstance(data[k], (np.ndarray, np.generic)):
                                raise Warning(
                                    "The data provided will only be flipped "
                                    "if it a list"
                                )
                            if not isinstance(data[k], list):
                                continue
                            d[k] = d[k][::-1]  # flip the data in the array

    def assign_cost_to_graph(self, cost_keys=["hop_distance"]):
        """
        Read the data dict on each add and populate a cost key
        Args:
            cost_keys: a list of keys for data on each edge.
                The SC Graph is decorated with a "cost" key that is the product of the different keys here
        """
        for k, v in self.unique_hops.items():
            cost_val = np.prod([v[ik] for ik in cost_keys])
            self.add_data_to_similar_edges(k, {"cost": cost_val})

    def get_intercollating_path(self, max_val=100000):
        """
        obtain an intercollating pathway through the material using hops that are in the current graph
        Basic idea:
            Get an endpoint p1 in the graph that is outside the current unit cell
            Ask the graph for a pathway that connects to p1 from either within the (0,0,0) cell
            or any other neighboring UC not containing p1.
        Args:
            max_val: Filter the graph by a cost

        Returns:
            Generator for List of Dicts:
            Each dict contains the information of a hop
        """

        if len(self.unique_hops) != len(self.unique_hops):
            logger.error(
                f"There are {len(self.unique_hops)} SC hops but {len(self.unique_hops)} UC hops in {self.name}"
            )

        # for u, v, k, d in self.s_graph.graph.edges(data=True, keys=True):
        for u in self.s_graph.graph.nodes():
            # for each hop that leave the UC cut that hop and get the path to other cell
            # if d["to_jimage"] == (0, 0, 0):
            #     continue
            # if d["cost"] > max_val:
            #     continue
            # Create a copy of the graph that is only composed of internal hops
            GG = deepcopy(self.s_graph.graph)
            # Trim this network so you cant loop back to the same node
            # print('----before', [(tmp_u, tmp_v, tmp_k, tmp_d['cost']) \
            #   for tmp_u, tmp_v, tmp_k, tmp_d in GG.edges(data=True, keys=True)])
            # GG.remove_edge(u, v, key=k)

            # Trim the higher cost edges from the network
            cut_edges = []
            for tmp_u, tmp_v, tmp_k, tmp_d in GG.edges(data=True, keys=True):
                if tmp_d["cost"] > max_val:
                    cut_edges.append((tmp_u, tmp_v, tmp_k))
            for tmp_u, tmp_v, tmp_k in cut_edges:
                GG.remove_edge(tmp_u, tmp_v, key=tmp_k)
            # iterating over all the edges
            # if an edge lands outside the boundary, we just ask the network for a path
            # that links the in-bound node of that edge and a in-bound copy of the final edge
            # print(f"HOP OUT : {u}, {v}")
            # for _, v, k, d in GG.edges(data=True, keys=True):
            #     print(_, v, k, d['to_jimage'], d['cost'])
            # pprint(_get_adjacency_with_images(GG.to_undirected()))
            best_ans, path_parent = periodic_dijkstra(
                GG, sources={u}, weight="cost", max_image=1
            )
            all_paths = []
            for idx, jimage in path_parent.keys():
                if idx == u and jimage != (0, 0, 0):
                    path = [*get_optimal_pathway_rev(path_parent, (idx, jimage))][::-1]
                    assert path[-1][0] == u
                    all_paths.append(path)

            if len(all_paths) == 0:
                continue
            # The first hop must be one that leaves the 000 unit cell
            path = min(all_paths, key=lambda x: best_ans[x[-1]])

            # get the sequence of MigrationPaths objects the represent the pathway
            path_hops = []
            for (idx1, jimage1), (idx2, jimage2) in zip(path[:-1], path[1:]):
                # for each pair of points in the periodic graph path look for end points in the original graph
                # the index pair has to be u->v with u <= v
                # once that is determined look up all such pairs in the graph and see if relative image
                # displacement +/- (jimage1 - jimage2) is present on of of the edges
                # Note: there should only ever be one valid to_jimage for a u->v pair
                i1_, i2_ = sorted((idx1, idx2))
                all_edge_data = [*GG.get_edge_data(i1_, i2_, default={}).items()]
                image_diff = np.subtract(jimage2, jimage1)
                found_ = 0
                for k, tmp_d in all_edge_data:
                    if tmp_d["to_jimage"] in {tuple(image_diff), tuple(-image_diff)}:
                        path_hops.append(tmp_d)
                        found_ += 1
                if found_ != 1:
                    raise RuntimeError("More than on edge mathched in original graph.")
            yield u, path_hops

            # # # if a None is present it means we have a loop.
            # if None not in path_hops:
            #     # The periodic nature of the graph is very difficult to deal with during path finding
            #     # If you have points hops (using a 10x10 periodic cell):
            #     # [B] (6,0) -> [C] (5, 1)
            #     # [C] (5,1) -> [A] (5, -1)
            #     # [A] (5,9) -> [B] (6, 10) # pbc wrapped not noticed by pathfinding
            #     #
            #
            #     yield path_hops

    def modify_path(self, paths):
        """
        This takes the results of get_intercollating_path() and turn them into an ordered path:
        The last hop is out of the unit cell, all previous hops stay within the unitcell

        Returns:
        list of ordered paths
        """

        for one_path in paths:
            if one_path[0][2]["to_jimage"] != (0, 0, 0):
                one_path = one_path[::-1]

            m_path = []

            if set(one_path[0][0:2]) == set(
                one_path[1][0:2]
            ):  # in case there is only 2 hops
                if one_path[0][0] == one_path[1][0]:
                    new_pos0 = one_path[0][1]
                    new_pos1 = one_path[0][0]
                    new_hop = (new_pos0, new_pos1, one_path[0][2])
                    m_path.append(new_hop)
                    m_path.append(one_path[1])
            else:
                start = list(set(one_path[0][0:2]) & set(one_path[-1][0:2]))[0]
                previous_point = start
                for one_hop_info in one_path:
                    one_hop = one_hop_info[0:2]
                    if one_hop[0] == previous_point:
                        m_path.append(one_hop_info)
                    if one_hop[1] == previous_point:
                        i_point = one_hop[1]
                        e_point = one_hop[0]
                        info = one_hop_info[2]
                        new_info = copy.deepcopy(info)
                        new_info["to_jimage"] = tuple([-u for u in info["to_jimage"]])
                        new_hop = (i_point, e_point, new_info)
                        m_path.append(new_hop)

                    previous_point = m_path[-1][1]

            yield m_path


class ComputedEntryPath(FullPathMapper):
    """
    Generate the full migration network using computed entires for intercollation andvacancy limits
    - Map the relaxed sites of a material back to the empty host lattice
    - Apply symmetry operations of the empty lattice to obtain the other positions of the intercollated atom
    - Get the symmetry inequivalent hops
    - Get the migration barriers for each inequivalent hop
    """

    def __init__(
        self,
        base_struct_entry,
        single_cat_entries,
        migrating_specie,
        base_aeccar=None,
        max_path_length=4,
        ltol=0.2,
        stol=0.3,
        symprec=0.1,
        angle_tol=5,
        full_sites_struct=None,
    ):
        """
        Pass in a entries for analysis

        Args:
          base_struct_entry: the structure without a working ion for us to analyze the migration
          single_cat_entries: list of structures containing a single cation at different positions
          base_aeccar: Chgcar object that contains the AECCAR0 + AECCAR2 (Default value = None)
          migration_specie: a String symbol or Element for the cation. (Default value = 'Li')
          ltol: parameter for StructureMatcher (Default value = 0.2)
          stol: parameter for StructureMatcher (Default value = 0.3)
          symprec: parameter for SpacegroupAnalyzer (Default value = 0.3)
          angle_tol: parameter for StructureMatcher (Default value = 5)
        """

        self.single_cat_entries = single_cat_entries
        self.base_struct_entry = base_struct_entry
        self.base_aeccar = base_aeccar
        self.migrating_specie = migrating_specie
        self.ltol = ltol
        self.stol = stol
        self.symprec = symprec
        self.angle_tol = angle_tol
        self.angle_tol = angle_tol
        self._tube_radius = None
        self.full_sites_struct = full_sites_struct
        self.sm = StructureMatcher(
            comparator=ElementComparator(),
            primitive_cell=False,
            ignored_species=[migrating_specie],
            ltol=ltol,
            stol=stol,
            angle_tol=angle_tol,
        )

        logger.debug("See if the structures all match")
        fit_ents = []
        if full_sites_struct:
            self.full_sites = full_sites_struct
            self.base_structure_full_sites = self.full_sites.copy()
            self.base_structure_full_sites.sites.extend(
                self.base_struct_entry.structure.sites
            )
        else:
            for ent in self.single_cat_entries:
                if self.sm.fit(self.base_struct_entry.structure, ent.structure):
                    fit_ents.append(ent)
            self.single_cat_entries = fit_ents

            self.translated_single_cat_entries = list(
                map(self.match_ent_to_base, self.single_cat_entries)
            )
            self.full_sites = self.get_full_sites()
            self.base_structure_full_sites = self.full_sites.copy()
            self.base_structure_full_sites.sites.extend(
                self.base_struct_entry.structure.sites
            )

        # Initialize
        super(ComputedEntryPath, self).__init__(
            structure=self.base_structure_full_sites,
            migrating_specie=migrating_specie,
            max_path_length=max_path_length,
            symprec=symprec,
            vac_mode=False,
            name=base_struct_entry.entry_id,
        )

        self.populate_edges_with_migration_paths()
        self.group_and_label_hops()
        self._populate_unique_hops_dict()
        if base_aeccar:
            self._setup_grids()

    def from_dbs(self):
        """
        Populate the object using entries from MP-like databases
        """

    def _from_dbs(self):
        """
        Populate the object using entries from MP-like databases

        """

    def match_ent_to_base(self, ent):
        """
        Transform the structure of one entry to match the base structure

        Args:
          ent:

        Returns:
          ComputedStructureEntry: entry with modified structure

        """
        new_ent = deepcopy(ent)
        new_struct = self.sm.get_s2_like_s1(
            self.base_struct_entry.structure, ent.structure
        )
        new_ent.structure = new_struct
        return new_ent

    def get_full_sites(self):
        """
        Get each group of symmetry inequivalent sites and combine them

        Args:

        Returns: a Structure with all possible Li sites, the enregy of the structure is stored as a site property

        """
        res = []
        for itr in self.translated_single_cat_entries:
            sub_site_list = get_all_sym_sites(
                itr,
                self.base_struct_entry,
                self.migrating_specie,
                symprec=self.symprec,
                angle_tol=self.angle_tol,
            )
            # ic(sub_site_list._sites)
            res.extend(sub_site_list._sites)
        # check to see if the sites collide with the base struture
        filtered_res = []
        for itr in res:
            col_sites = self.base_struct_entry.structure.get_sites_in_sphere(
                itr.coords, BASE_COLLISION_R
            )
            if len(col_sites) == 0:
                filtered_res.append(itr)
        res = Structure.from_sites(filtered_res)
        if len(res) > 1:
            res.merge_sites(tol=SITE_MERGE_R, mode="average")
        return res

    def _setup_grids(self):
        """Populate the internal varialbes used for defining the grid points in the charge density analysis"""

        # set up the grid
        aa = np.linspace(0, 1, len(self.base_aeccar.get_axis_grid(0)), endpoint=False)
        bb = np.linspace(0, 1, len(self.base_aeccar.get_axis_grid(1)), endpoint=False)
        cc = np.linspace(0, 1, len(self.base_aeccar.get_axis_grid(2)), endpoint=False)
        # move the grid points to the center
        aa, bb, dd = map(_shift_grid, [aa, bb, cc])

        # mesh grid for each unit cell
        AA, BB, CC = np.meshgrid(aa, bb, cc, indexing="ij")

        # should be using a mesh grid of 5x5x5 (using 3x3x3 misses some fringe cases)
        # but using 3x3x3 is much faster and only crops the cyliners in some rare case
        # if you keep the tube_radius small then this is not a big deal
        IMA, IMB, IMC = np.meshgrid([-1, 0, 1], [-1, 0, 1], [-1, 0, 1], indexing="ij")

        # store these
        self._uc_grid_shape = AA.shape
        self._fcoords = np.vstack([AA.flatten(), BB.flatten(), CC.flatten()]).T
        self._images = np.vstack([IMA.flatten(), IMB.flatten(), IMC.flatten()]).T

    def _dist_mat(self, pos_frac):
        # return a matrix that contains the distances to pos_frac
        aa = np.linspace(0, 1, len(self.base_aeccar.get_axis_grid(0)), endpoint=False)
        bb = np.linspace(0, 1, len(self.base_aeccar.get_axis_grid(1)), endpoint=False)
        cc = np.linspace(0, 1, len(self.base_aeccar.get_axis_grid(2)), endpoint=False)
        aa, bb, cc = map(_shift_grid, [aa, bb, cc])
        AA, BB, CC = np.meshgrid(aa, bb, cc, indexing="ij")
        dist_from_pos = self.base_aeccar.structure.lattice.get_all_distances(
            fcoords1=np.vstack([AA.flatten(), BB.flatten(), CC.flatten()]).T,
            fcoords2=pos_frac,
        )
        return dist_from_pos.reshape(AA.shape)

    def _get_pathfinder_from_hop(self, migration_path, n_images=20):
        # get migration pathfinder objects which contains the paths
        ipos = migration_path.isite.frac_coords
        epos = migration_path.esite.frac_coords
        mpos = migration_path.esite.frac_coords

        start_struct = self.base_aeccar.structure.copy()
        end_struct = self.base_aeccar.structure.copy()
        mid_struct = self.base_aeccar.structure.copy()

        # the moving ion is always inserted on the zero index
        start_struct.insert(0, self.migrating_specie, ipos, properties=dict(magmom=0))
        end_struct.insert(0, self.migrating_specie, epos, properties=dict(magmom=0))
        mid_struct.insert(0, self.migrating_specie, mpos, properties=dict(magmom=0))

        chgpot = ChgcarPotential(self.base_aeccar, normalize=False)
        npf = NEBPathfinder(
            start_struct,
            end_struct,
            relax_sites=[0],
            v=chgpot.get_v(),
            n_images=n_images,
            mid_struct=mid_struct,
        )
        return npf

    def _get_avg_chg_at_max(
        self, migration_path, radius=None, chg_along_path=False, output_positions=False
    ):
        """obtain the maximum average charge along the path

        Args:
            migration_path (MigrationPath): MigrationPath object that represents a given hop
            radius (float, optional): radius of sphere to perform the average.
                    Defaults to None, which used the _tube_radius instead
            chg_along_path (bool, optional): If True, also return the entire list of average
                    charges along the path for plotting.
                    Defaults to False.
            output_positions (bool, optional): If True, also return the entire list of average
                    charges along the path for plotting.
                    Defaults to False.

        Returns:
            [float]: maximum of the charge density, (optional: entire list of charge density)
        """
        if radius is None:
            rr = self._tube_radius
        if not rr > 0:
            raise ValueError("The integration radius must be positive.")

        npf = self._get_pathfinder_from_hop(migration_path)
        # get the charge in a sphere around each point
        centers = [image.sites[0].frac_coords for image in npf.images]
        avg_chg = []
        for ict in centers:
            dist_mat = self._dist_mat(ict)
            mask = dist_mat < rr
            vol_sphere = self.base_aeccar.structure.volume * (
                mask.sum() / self.base_aeccar.ngridpts
            )
            avg_chg.append(
                np.sum(self.base_aeccar.data["total"] * mask)
                / self.base_aeccar.ngridpts
                / vol_sphere
            )
        if output_positions:
            return max(avg_chg), avg_chg, centers
        elif chg_along_path:
            return max(avg_chg), avg_chg
        else:
            return max(avg_chg)

    def _get_chg_between_sites_tube(self, migration_path, mask_file_seedname=None):
        """
        Calculate the amount of charge that a migrating ion has to move through in order to complete a hop

        Args:
            migration_path: MigrationPath object that represents a given hop
            mask_file_seedname(string): seedname for output of the migration path masks (for debugging and
                visualization) (Default value = None)

        Returns:
            float: The total charge density in a tube that connects two sites of a given edges of the graph

        """
        try:
            self._tube_radius
        except NameError:
            logger.warning(
                "The radius of the tubes for charge analysis need to be defined first."
            )
        ipos = migration_path.isite.frac_coords
        epos = migration_path.esite.frac_coords
        if not self.base_aeccar:
            return 0

        cart_ipos = np.dot(ipos, self.base_aeccar.structure.lattice.matrix)
        cart_epos = np.dot(epos, self.base_aeccar.structure.lattice.matrix)
        pbc_mask = np.zeros(self._uc_grid_shape, dtype=bool).flatten()
        for img in self._images:
            grid_pos = np.dot(
                self._fcoords + img, self.base_aeccar.structure.lattice.matrix
            )
            proj_on_line = np.dot(grid_pos - cart_ipos, cart_epos - cart_ipos) / (
                np.linalg.norm(cart_epos - cart_ipos)
            )
            dist_to_line = np.linalg.norm(
                np.cross(grid_pos - cart_ipos, cart_epos - cart_ipos)
                / (np.linalg.norm(cart_epos - cart_ipos)),
                axis=-1,
            )

            mask = (
                (proj_on_line >= 0)
                * (proj_on_line < np.linalg.norm(cart_epos - cart_ipos))
                * (dist_to_line < self._tube_radius)
            )
            pbc_mask = pbc_mask + mask
        pbc_mask = pbc_mask.reshape(self._uc_grid_shape)

        if mask_file_seedname:
            mask_out = VolumetricData(
                structure=self.base_aeccar.structure.copy(),
                data={"total": self.base_aeccar.data["total"]},
            )
            mask_out.structure.insert(0, "X", ipos)
            mask_out.structure.insert(0, "X", epos)
            mask_out.data["total"] = pbc_mask
            isym = self.symm_structure.wyckoff_symbols[migration_path.iindex]
            esym = self.symm_structure.wyckoff_symbols[migration_path.eindex]
            mask_out.write_file(
                "{}_{}_{}_tot({:0.2f}).vasp".format(
                    mask_file_seedname, isym, esym, mask_out.data["total"].sum()
                )
            )

        return (
            self.base_aeccar.data["total"][pbc_mask].sum()
            / self.base_aeccar.ngridpts
            / self.base_aeccar.structure.volume
        )

    def populate_edges_with_chg_density_info(self, tube_radius=1):
        self._tube_radius = tube_radius
        for k, v in self.unique_hops.items():
            # charge in tube
            chg_tot = self._get_chg_between_sites_tube(v["hop"])
            self.add_data_to_similar_edges(k, {"chg_total": chg_tot})

            # max charge in sphere
            max_chg, avg_chg_list, frac_coords_list = self._get_avg_chg_at_max(
                v["hop"], chg_along_path=True, output_positions=True
            )
            images = [
                {"position": ifrac, "average_charge": ichg}
                for ifrac, ichg in zip(frac_coords_list, avg_chg_list)
            ]
            v.update(
                dict(
                    chg_total=chg_tot,
                    max_avg_chg=max_chg,
                    images=images,
                )
            )
            self.add_data_to_similar_edges(k, {"max_avg_chg": max_chg})

    def get_least_chg_path(self):
        """
        obtain an intercollating pathway through the material that has the least amount of charge
        Returns:
            list of hops
        """
        min_chg = 100000000
        min_path = []
        all_paths = self.get_intercollating_path()
        for path in all_paths:
            sum_chg = np.sum([hop[2]["chg_total"] for hop in path])
            sum_length = np.sum([hop[2]["hop"].length for hop in path])
            avg_chg = sum_chg / sum_length
            if avg_chg < min_chg:
                min_chg = sum_chg
                min_path = path
        return min_path

    def get_summary_dict(self):
        """
        Dictionary format, for saving to database
        """
        hops = []
        for u, v, d in self.s_graph.graph.edges(data=True):
            dd = defaultdict(lambda: None)
            dd.update(d)
            hops.append(
                dict(
                    hop_label=dd["hop_label"],
                    iinddex=u,
                    einddex=v,
                    to_jimage=dd["to_jimage"],
                    ipos=dd["ipos"],
                    epos=dd["epos"],
                    ipos_cart=dd["ipos_cart"],
                    epos_cart=dd["epos_cart"],
                    max_avg_chg=dd["max_avg_chg"],
                    chg_total=dd["chg_total"],
                )
            )
        unique_hops = []
        for k, d in self.unique_hops.items():
            dd = defaultdict(lambda: None)
            dd.update(d)
            unique_hops.append(
                dict(
                    hop_label=dd["hop_label"],
                    iinddex=dd["iinddex"],
                    einddex=dd["einddex"],
                    to_jimage=dd["to_jimage"],
                    ipos=dd["ipos"],
                    epos=dd["epos"],
                    ipos_cart=dd["ipos_cart"],
                    epos_cart=dd["epos_cart"],
                    max_avg_chg=dd["max_avg_chg"],
                    chg_total=dd["chg_total"],
                    images=dd["images"],
                )
            )
        unique_hops = sorted(unique_hops, key=lambda x: x["hop_label"])
        return dict(
            base_task_id=self.base_struct_entry.entry_id,
            base_structure=self.base_struct_entry.structure.as_dict(),
            inserted_ids=[ent.entry_id for ent in self.single_cat_entries],
            migrating_specie=self.migrating_specie.name,
            max_path_length=self.max_path_length,
            ltol=self.ltol,
            stol=self.stol,
            full_sites_struct=self.full_sites.as_dict(),
            angle_tol=self.angle_tol,
            hops=hops,
            unique_hops=unique_hops,
        )


def get_all_sym_sites(
    ent, base_struct_entry, migrating_specie, symprec=2.0, angle_tol=10
):
    """
    Return all of the symmetry equivalent sites by applying the symmetry operation of the empty structure

    Args:
        ent(ComputedStructureEntry): that contains cation
        migrating_species(string or Elment):

    Returns:
        Structure: containing all of the symmetry equivalent sites

    """
    migrating_specie_el = get_el_sp(migrating_specie)
    sa = SpacegroupAnalyzer(
        base_struct_entry.structure, symprec=symprec, angle_tolerance=angle_tol
    )
    # start with the base structure but empty
    host_allsites = base_struct_entry.structure.copy()
    host_allsites.remove_species(host_allsites.species)
    pos_Li = list(
        filter(
            lambda isite: isite.species_string == migrating_specie_el.name,
            ent.structure.sites,
        )
    )

    # energy difference per site
    inserted_energy = (ent.energy - base_struct_entry.energy) / len(pos_Li)

    for isite in pos_Li:
        host_allsites.insert(
            0,
            migrating_specie_el.name,
            np.mod(isite.frac_coords, 1),
            properties=dict(inserted_energy=inserted_energy, magmom=0),
        )
    # base_ops = sa.get_space_group_operations()
    # all_ops = generate_full_symmops(base_ops, tol=1.0)
    for op in sa.get_space_group_operations():
        logger.debug(f"{op}")
        struct_tmp = host_allsites.copy()
        struct_tmp.apply_operation(symmop=op, fractional=True)
        for isite in struct_tmp.sites:
            if isite.species_string == migrating_specie_el.name:
                logger.debug(f"{op}")
                host_allsites.insert(
                    0,
                    migrating_specie_el.name,
                    np.mod(isite.frac_coords, 1),
                    properties=dict(inserted_energy=inserted_energy, magmom=0),
                )
                host_allsites.merge_sites(
                    tol=SITE_MERGE_R, mode="delete"
                )  # keeps only remove duplicates
    return host_allsites


# Utility functions


def _shift_grid(vv):
    """
    Move the grid points by half a step so that they sit in the center

    Args:
        vv: equally space grid points in 1-D

    """
    step = vv[1] - vv[0]
    return vv + step / 2.0


def get_hop_site_sequence(hop_list: List[Dict], start_u: Union[int, str]) -> List:
    """
    Read in a list of hop dictionaries and print the sequence of sites.
    Args:
        hop_list: a list of the data on a sequence of hops
        start_u: the site index of the starting sites
    Returns:
        String representation of the hop sequence
    """
    hops = iter(hop_list)
    ihop = next(hops)
    if ihop["eindex"] == start_u:
        site_seq = [ihop["eindex"], ihop["iindex"]]
    else:
        site_seq = [ihop["iindex"], ihop["eindex"]]

    for ihop in hops:
        if ihop["iindex"] == site_seq[-1]:
            site_seq.append(ihop["eindex"])
        elif ihop["eindex"] == site_seq[-1]:
            site_seq.append(ihop["iindex"])
        else:
            raise RuntimeError("The sequence of sites for the path is invalid.")
    return site_seq


"""
Note the current pathway algorithm no longer needs supercells but the following
functions might still be useful for other applications

Finding all possible pathways in the periodic network is not possible.
We can do a good enough job if we make a (2x2x2) supercell of the structure and find
migration events using the following procedure:

- Look for a hop that leaves the SC like A->B (B on the outside)
- then at A look for a pathway to the image of B inside the SC
"""

# Utility Functions for comparing UC and SC hops


def almost(a, b):
    # return true if the values are almost equal
    SMALL_VAL = 1e-4
    try:
        return all([almost(i, j) for i, j in zip(list(a), list(b))])
    except BaseException:
        if (isinstance(a, float) or isinstance(a, int)) and (
            isinstance(b, float) or isinstance(b, int)
        ):
            return abs(a - b) < SMALL_VAL
        else:
            raise NotImplementedError


def check_uc_hop(sc_hop, uc_hop):
    """
    See if hop in the 2X2X2 supercell and a unit cell hop
    are equilvalent under lattice translation

    Args:
        sc_hop: MigrationPath object form pymatgen-diffusion.
        uc_hop: MigrationPath object form pymatgen-diffusion.
    Return:
        image vector of lenght 3
        Is the UC hop flip of the SC hop

    """

    directions = np.array(
        [
            [0, 0, 0],
            [1, 0, 0],
            [0, 1, 0],
            [0, 0, 1],
            [0, 1, 1],
            [1, 0, 1],
            [1, 1, 0],
            [1, 1, 1],
        ]
    )

    sc_ipos = [icoord * 2 for icoord in sc_hop.isite.frac_coords]
    sc_epos = [icoord * 2 for icoord in sc_hop.esite.frac_coords]
    sc_mpos = [icoord * 2 for icoord in sc_hop.msite.frac_coords]

    uc_ipos = uc_hop.isite.frac_coords
    uc_epos = uc_hop.esite.frac_coords
    uc_mpos = uc_hop.msite.frac_coords

    for idir in directions:
        tmp_m = uc_mpos + idir
        if almost(tmp_m, sc_mpos):
            tmp_i = uc_ipos + idir
            tmp_e = uc_epos + idir
            if almost(tmp_i, sc_ipos) and almost(tmp_e, sc_epos):
                return idir, False
            elif almost(tmp_e, sc_ipos) and almost(tmp_i, sc_epos):
                return idir, True
    return None


def map_hop_sc2uc(sc_hop, fpm_uc):
    """
    Map a given hop in the SC onto the UC.

    Args:
        sc_hop: MigrationPath object form pymatgen-diffusion.
        fpm_uc: FullPathMapper object from pymatgen-diffusion.

    Note:
        For now assume that the SC is exactly 2x2x2 of the UC.
        Can add in the parsing of different SC's later

        For a migration event in the SC from (0.45,0,0)-->(0.55,0,0)
        the UC hop might be (0.9,0,0)-->(0.1,0,0)[img:100]
        for the inverse of (0.1,0,0)-->(-0.1,0,0) the code needs to account for both those cases
    """
    for u, v, d in fpm_uc.s_graph.graph.edges(data=True):
        chk_res = check_uc_hop(sc_hop=sc_hop, uc_hop=d["hop"])
        if chk_res is not None:
            assert almost(d["hop"].length, sc_hop.length)
            return dict(
                uc_u=u,
                uc_v=v,
                hop=d["hop"],
                shift=chk_res[0],
                flip=chk_res[1],
                hop_label=d["hop_label"],
            )
    raise AssertionError("Looking for a SC hop without a matching UC hop")
